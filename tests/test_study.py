"""Tests for study.py. All data here is made up; no real Kalshi data is needed to run them."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import study


def contract(game, yes_ask, no_ask, result, player="A Player", league="NFL", hours_before=1.0,
             books_over=0.5, line=10.5, middle=10.0, distance=0.5):
    """One made-up row with the columns study.py reads."""
    kickoff = pd.Timestamp("2026-10-11T17:00:00Z")
    return {
        "league": league, "event_id": game, "player": player,
        "yes_ask": yes_ask, "no_ask": no_ask, "result": result,
        "books_over": books_over, "line": line, "middle": middle, "distance": distance,
        "game_time": kickoff.isoformat(),
        "logged_at": (kickoff - pd.Timedelta(hours=hours_before)).isoformat(),
    }


def prepared(rows):
    return study.prepare(pd.DataFrame(rows))


def test_midpoint_is_halfway_between_bid_and_ask():
    # YES costs 12c; NO costs 90c, so YES can be sold at 10c. Midpoint 11c.
    df = prepared([contract("g1", 0.12, 0.90, "no")])
    assert df["midpoint"].iloc[0] == pytest.approx(0.11)


def test_missing_bid_gives_no_midpoint_and_is_left_out_of_the_table():
    df = prepared([contract("g1", 0.12, None, "no"), contract("g1", 0.31, 0.71, "yes")])
    assert df["midpoint"].isna().sum() == 1
    assert study.band_table(df, resamples=50)["contracts"].sum() == 1


def test_part_way_settlements_are_left_out():
    df = prepared([contract("g1", 0.30, 0.72, "scalar"), contract("g1", 0.30, 0.72, "yes")])
    assert len(df) == 1
    assert df["happened"].tolist() == [1]


@pytest.mark.parametrize("yes_ask, no_ask, band", [
    (0.05, 0.97, "under 5c"),    # midpoint 4c
    (0.06, 0.96, "5-10c"),       # midpoint exactly 5c: the lower edge belongs to the band
    (0.10, 0.91, "5-10c"),       # midpoint 9.5c
    (0.11, 0.91, "10-20c"),      # midpoint exactly 10c
    (0.51, 0.51, "50-80c"),      # midpoint exactly 50c
    (0.81, 0.21, "80c and up"),  # midpoint exactly 80c
    (0.99, 0.02, "80c and up"),
])
def test_band_edges(yes_ask, no_ask, band):
    df = prepared([contract("g1", yes_ask, no_ask, "no")])
    assert df["band"].iloc[0] == band


def test_fresh_means_scanned_within_six_hours_of_the_start():
    df = prepared([contract("g1", 0.3, 0.72, "no", hours_before=6.0),
                   contract("g1", 0.3, 0.72, "no", hours_before=6.5)])
    assert df["fresh"].tolist() == [True, False]


def test_fee_is_rounded_up_to_the_cent_on_the_whole_order():
    assert study.fee_per_contract(0.20, contracts=1) == pytest.approx(0.02)     # 1.12c -> 2c
    assert study.fee_per_contract(0.05, contracts=1) == pytest.approx(0.01)     # 0.33c -> 1c
    assert study.fee_per_contract(0.50, contracts=100) == pytest.approx(0.0175)  # exactly 175c, no rounding
    assert study.fee_per_contract(0.20, contracts=10) == pytest.approx(0.012)   # 11.2c -> 12c


def test_return_from_buying_yes():
    # Two 50c contracts, one pays $1. Cost 2 x (0.50 + 0.02 fee) = 1.04, payout 1.00.
    df = prepared([contract("g1", 0.50, 0.52, "yes"), contract("g1", 0.50, 0.52, "no")])
    assert study.yes_buy_return(df, contracts=1) == pytest.approx((1.00 - 1.04) / 1.04)


def four_contracts_at_30c():
    # YES costs 32c and sells at 28c: midpoint 30c, spread 2c each way. One of four pays out.
    # Fee on a 10-contract order at 32c: 15.232c, rounded up to 16c, so 1.6c a contract.
    return [contract("g1", 0.32, 0.72, r) for r in ["yes", "no", "no", "no"]]


def test_cost_split_numbers():
    table = study.cost_table(prepared(four_contracts_at_30c()), contracts=10, resamples=50)
    row = table[table["band"] == "20-50c"].iloc[0]
    spent = 0.32 + 0.016
    assert row["half_spread_cents"] == pytest.approx(2.0)
    assert row["fee_cents"] == pytest.approx(1.6)
    assert row["midpoint_miss_cents"] == pytest.approx(5.0)   # priced at 30%, happened 25%
    assert row["lost_to_half_spread"] == pytest.approx(0.02 / spent)
    assert row["lost_to_fee"] == pytest.approx(0.016 / spent)
    assert row["lost_to_midpoint"] == pytest.approx(0.05 / spent)
    assert row["return"] == pytest.approx((0.25 - spent) / spent)
    assert row["return_before_fee"] == pytest.approx((0.25 - 0.32) / 0.32)


def test_the_three_parts_add_up_to_the_loss():
    rows = four_contracts_at_30c()
    rows += [contract("g2", 0.07, 0.96, "no"), contract("g2", 0.07, 0.96, "yes"),
             contract("g3", 0.90, 0.13, "yes"), contract("g3", 0.55, 0.47, "no")]
    df = prepared(rows)
    table = study.cost_table(df, resamples=50)
    parts = table["lost_to_half_spread"] + table["lost_to_fee"] + table["lost_to_midpoint"]
    assert (parts + table["return"]).abs().max() < 1e-12
    # the "all prices" row is the same as buying every contract
    everything = table[table["band"] == study.ALL_PRICES].iloc[0]
    assert everything["contracts"] == len(df)
    assert everything["return"] == pytest.approx(study.yes_buy_return(df))


def test_no_midpoint_loss_when_the_midpoint_is_right():
    # Midpoint 50c and half pay out: everything lost is spread and fee.
    rows = [contract("g1", 0.52, 0.52, "yes"), contract("g1", 0.52, 0.52, "no")]
    row = study.cost_table(prepared(rows), resamples=50).iloc[0]
    assert row["lost_to_midpoint"] == pytest.approx(0.0)
    assert row["return"] < 0


def test_return_range_keeps_games_together():
    # Game A's contracts all pay out, game B's never do, so the return swings with which games
    # are drawn: from losing everything to nearly doubling the money.
    rows = [contract("A", 0.51, 0.51, "yes") for _ in range(10)]
    rows += [contract("B", 0.51, 0.51, "no") for _ in range(10)]
    row = study.cost_table(prepared(rows), resamples=500).iloc[0]
    assert row["return_low"] == pytest.approx(-1.0)
    assert row["return_high"] > 0.8


def test_credibility():
    # At a 50% chance the standard is 1,082 contracts; a quarter of that gives half credibility.
    assert study.credibility(1082, 0.5) == pytest.approx(1.0)
    assert study.credibility(1082 / 4, 0.5) == pytest.approx(0.5)
    assert study.credibility(100_000, 0.5) == 1.0
    # A 5% event needs 1,082 x 0.95 / 0.05 = 20,558 contracts.
    assert study.credibility(20_558, 0.05) == pytest.approx(1.0, abs=1e-4)


def test_band_table_numbers():
    # Four contracts with a 30c midpoint in one game; one pays out. Actual 25%, expected 30%.
    rows = [contract("g1", 0.31, 0.71, r) for r in ["yes", "no", "no", "no"]]
    row = study.band_table(prepared(rows), resamples=50).iloc[0]
    assert row["band"] == "20-50c"
    assert (row["contracts"], row["games"], row["yes_outcomes"]) == (4, 1, 1)
    assert row["actual"] == pytest.approx(0.25)
    assert row["expected"] == pytest.approx(0.30)
    assert row["gap"] == pytest.approx(-0.05)
    assert row["actual_to_expected"] == pytest.approx(0.25 / 0.30)


def test_resampling_keeps_each_games_contracts_together():
    # Game A: all ten 50c contracts pay out. Game B: none do. Overall the gap is zero, but a
    # resample that keeps games whole can draw A twice (+50 points) or B twice (-50 points).
    # Resampling single contracts could never reach those extremes.
    rows = [contract("A", 0.51, 0.51, "yes") for _ in range(10)]
    rows += [contract("B", 0.51, 0.51, "no") for _ in range(10)]
    row = study.band_table(prepared(rows), resamples=500).iloc[0]
    assert row["gap"] == pytest.approx(0.0)
    assert row["gap_low"] == pytest.approx(-0.5)
    assert row["gap_high"] == pytest.approx(0.5)
    assert row["standard_error_ratio"] > 2
    assert row["credibility_by_game"] < row["credibility_if_independent"]


def test_no_range_when_every_game_shows_the_same_gap():
    rows = []
    for game in ["A", "B", "C"]:
        rows += [contract(game, 0.51, 0.51, "yes"), contract(game, 0.51, 0.51, "no")]
    row = study.band_table(prepared(rows), resamples=200).iloc[0]
    assert row["gap_low"] == pytest.approx(0.0)
    assert row["gap_high"] == pytest.approx(0.0)


def test_same_seed_gives_the_same_range():
    rows = [contract(f"g{i}", 0.31, 0.71, "yes" if i % 3 == 0 else "no") for i in range(12)]
    first = study.band_table(prepared(rows), resamples=200)
    second = study.band_table(prepared(rows), resamples=200)
    pd.testing.assert_frame_equal(first, second)


def test_games_with_the_same_id_in_different_leagues_stay_separate():
    df = prepared([contract("g1", 0.31, 0.71, "no", league="NFL"),
                   contract("g1", 0.31, 0.71, "no", league="NBA")])
    assert df["game_id"].nunique() == 2


def test_brier_scores():
    # Kalshi's midpoint says 30% for both; the book curve says 50%. One pays out, one doesn't.
    rows = [contract("g1", 0.31, 0.71, "yes", books_over=0.5), contract("g2", 0.31, 0.71, "no", books_over=0.5)]
    row = study.accuracy_table(prepared(rows), resamples=50).iloc[0]
    assert row["brier_kalshi"] == pytest.approx((0.7 ** 2 + 0.3 ** 2) / 2)   # 0.29
    assert row["brier_book"] == pytest.approx(0.25)
    assert row["brier_base_rate"] == pytest.approx(0.25)                      # half paid out
    assert row["kalshi_minus_book"] == pytest.approx(0.04)
    assert (row["contracts"], row["games"]) == (2, 2)


def test_brier_difference_has_no_range_when_every_game_is_the_same():
    rows = []
    for game in ["A", "B", "C"]:
        rows += [contract(game, 0.31, 0.71, "yes"), contract(game, 0.31, 0.71, "no")]
    row = study.accuracy_table(prepared(rows), resamples=200).iloc[0]
    assert row["kalshi_minus_book_low"] == pytest.approx(row["kalshi_minus_book"])
    assert row["kalshi_minus_book_high"] == pytest.approx(row["kalshi_minus_book"])


@pytest.mark.parametrize("line, middle, distance, band", [
    (12.5, 10.0, 2.5, "2+ above"),
    (12.0, 10.0, 2.0, "2+ above"),     # exactly 2 swings above: the lower edge belongs to the band
    (11.5, 10.0, 1.5, "1-2 above"),
    (10.5, 10.0, 0.5, "0-1 above"),
    (9.5, 10.0, 0.5, "0-1 below"),     # the line is under the middle, so the distance counts as negative
    (9.0, 10.0, 1.0, "0-1 below"),
    (8.5, 10.0, 1.5, "1+ below"),
])
def test_distance_bands_use_which_side_of_the_middle_the_line_is_on(line, middle, distance, band):
    df = prepared([contract("g1", 0.31, 0.71, "no", line=line, middle=middle, distance=distance)])
    assert df["distance_band"].iloc[0] == band


def test_rare_table_judges_each_source_on_its_own_rare_contracts():
    rows = [
        # the book curve calls these two rare (2% each); Kalshi's midpoint (11%) does not
        contract("g1", 0.12, 0.90, "yes", books_over=0.02),
        contract("g1", 0.12, 0.90, "no", books_over=0.02),
        # Kalshi's midpoint calls this one rare (3%); the book curve (8%) does not
        contract("g2", 0.04, 0.98, "no", books_over=0.08),
    ]
    table = study.rare_table(prepared(rows), resamples=50).set_index("source")
    assert table.loc[study.BOOK, "contracts"] == 2
    assert table.loc[study.BOOK, "actual"] == pytest.approx(0.5)
    assert table.loc[study.BOOK, "expected"] == pytest.approx(0.02)
    assert table.loc[study.BOOK, "actual_to_expected"] == pytest.approx(25.0)
    assert table.loc[study.KALSHI, "contracts"] == 1
    assert table.loc[study.KALSHI, "expected"] == pytest.approx(0.03)
    assert table.loc[study.KALSHI, "yes_outcomes"] == 0


def test_distance_table_compares_both_sources_on_the_same_contracts():
    far = dict(line=13.0, middle=10.0, distance=3.0, books_over=0.01)
    rows = [contract("g1", 0.06, 0.96, "yes", **far)] + [contract("g1", 0.06, 0.96, "no", **far) for _ in range(9)]
    row = study.distance_table(prepared(rows), resamples=50).iloc[0]
    assert row["distance_band"] == "2+ above"
    assert row["contracts"] == 10
    assert row["actual"] == pytest.approx(0.10)
    assert row["kalshi_gap"] == pytest.approx(0.10 - 0.05)   # midpoint 5c
    assert row["book_gap"] == pytest.approx(0.10 - 0.01)


def test_dropping_one_game_at_a_time():
    # Game A's two 30c contracts both pay out; game B's two do not. Actual minus expected is
    # +20 points overall, +70 with only A left and -30 with only B left.
    low = dict(books_over=0.02, league="NBA")
    rows = [contract("A", 0.05, 0.97, "yes", **low), contract("A", 0.05, 0.97, "yes", **low),
            contract("B", 0.05, 0.97, "no", **low), contract("B", 0.05, 0.97, "no", **low)]
    table = study.sensitivity_table(prepared(rows)).set_index("key")
    gap = table.loc["nba_under5_gap"]
    assert gap["games"] == 2
    assert gap["full_sample"] == pytest.approx(0.5 - 0.04)
    assert gap["lowest_without_one_game"] == pytest.approx(0.0 - 0.04)    # A dropped
    assert gap["highest_without_one_game"] == pytest.approx(1.0 - 0.04)   # B dropped
    # the full-sample return matches buying every contract
    assert table.loc["return_all", "full_sample"] == pytest.approx(study.yes_buy_return(prepared(rows)))


def test_smaller_orders_pay_more_fee_per_contract():
    table = study.order_size_table(prepared(four_contracts_at_30c())).set_index("band")
    row = table.loc["20-50c"]
    assert row["no_fee"] == pytest.approx((0.25 - 0.32) / 0.32)
    assert row["contracts_10"] == pytest.approx((0.25 - 0.336) / 0.336)   # 1.6c fee a contract
    assert row["contracts_1"] == pytest.approx((0.25 - 0.34) / 0.34)      # 1.52c rounded up to 2c
    assert row["no_fee"] > row["contracts_100"] > row["contracts_10"] > row["contracts_1"]


def test_whole_study_runs_on_the_made_up_sample_data(tmp_path):
    sample = Path(__file__).parent.parent / "sample_data"
    study.main([str(sample / "nfl_sample.csv"), str(sample / "nba_sample.csv"), "--out", str(tmp_path)])
    for name in ["output/band_table.csv", "output/cost_table.csv", "output/accuracy_table.csv",
                 "output/rare_table.csv", "output/distance_table.csv", "output/sensitivity_table.csv",
                 "output/order_size_table.csv", "output/key_numbers.json", "output/tables.md",
                 "charts/actual_vs_expected.png", "charts/cost_split.png", "charts/kalshi_vs_book_curve.png",
                 "one_page_summary.pdf"]:
        assert (tmp_path / name).stat().st_size > 0, name


def test_prices_logged_after_the_start_are_left_out():
    rows = [contract("g1", 0.31, 0.71, "yes", hours_before=0.0),     # logged exactly at the start: kept
            contract("g1", 0.31, 0.71, "yes", hours_before=1.0),
            contract("g1", 0.95, 0.07, "yes", hours_before=-0.5)]    # logged half an hour into the game
    df = prepared(rows)
    assert len(df) == 2
    assert (df["hours_before"] >= 0).all()
    assert df["yes_ask"].max() == pytest.approx(0.31)


def test_data_checks_account_for_every_row():
    rows = [contract("g1", 0.31, 0.71, "yes"), contract("g1", 0.31, 0.71, "no"),
            contract("g1", 0.31, None, "no"),                        # nobody selling NO: no midpoint
            contract("g1", 0.31, 0.71, "scalar"),                    # settled part-way
            contract("g1", 0.31, 0.71, None),                        # not settled yet
            contract("g1", 0.95, 0.07, "yes", hours_before=-1.0)]    # logged after the start
    raw = pd.DataFrame(rows).assign(ticker=["a", "b", "c", "d", "e", "e"])
    row = study.data_quality(raw).iloc[0]
    assert row["rows_read"] == 6
    assert (row["logged_after_start"], row["settled_part_way"], row["not_settled"]) == (1, 1, 1)
    assert (row["settled_yes_or_no"], row["no_price_to_sell"], row["in_the_analysis"]) == (3, 1, 2)
    assert row["duplicate_tickers"] == 1
    left_out = row["logged_after_start"] + row["settled_part_way"] + row["not_settled"]
    assert left_out + row["settled_yes_or_no"] == row["rows_read"]


def test_equal_weighting_of_player_games_and_games():
    # Player A has three 50c contracts that all pay out; player B, in another game, has one that
    # does not. Counting contracts, 3 of 4 paid out (+25 points against 50% expected). Giving
    # each player-game, or each game, equal weight it is 1 of 2 (0 points).
    rows = [contract("g1", 0.51, 0.51, "yes", player="A") for _ in range(3)]
    rows += [contract("g2", 0.51, 0.51, "no", player="B")]
    gap = study.weighting_table(prepared(rows)).set_index("key").loc["gap_all"]
    assert gap["by_contract"] == pytest.approx(0.25)
    assert gap["each_player_game_equal"] == pytest.approx(0.0)
    assert gap["each_game_equal"] == pytest.approx(0.0)


def test_good_input_passes_the_checks():
    study.validate(pd.DataFrame([contract("g1", 0.31, 0.71, "yes"), contract("g1", 0.31, None, None)]))


@pytest.mark.parametrize("change, message", [
    (dict(yes_ask=0.30, no_ask=0.60), "below the price to sell"),   # sells at 40c but buys at 30c
    (dict(yes_ask=1.20), "outside 0 to 1"),
    (dict(result="maybe"), "unknown result"),
    (dict(books_over=None), "book-curve chance"),
])
def test_bad_input_stops_the_study(change, message):
    row = contract("g1", 0.31, 0.71, "yes")
    row.update(change)
    with pytest.raises(ValueError, match=message):
        study.validate(pd.DataFrame([row]))


def test_missing_column_stops_the_study():
    raw = pd.DataFrame([contract("g1", 0.31, 0.71, "yes")]).drop(columns="line")
    with pytest.raises(ValueError, match="missing columns"):
        study.validate(raw)


def test_exclusion_table_compares_kept_and_left_out_contracts():
    rows = [contract("g1", 0.31, 0.71, "yes", books_over=0.3), contract("g1", 0.31, 0.71, "no", books_over=0.3),
            contract("g1", 0.04, None, "no", books_over=0.02), contract("g1", 0.12, None, "no", books_over=0.04)]
    table = study.exclusion_table(prepared(rows)).set_index("group")
    kept, left_out = table.loc["In the analysis"], table.loc["Left out: no price to sell YES"]
    assert (kept["contracts"], left_out["contracts"]) == (2, 2)
    assert kept["paid_out"] == pytest.approx(0.5)
    assert left_out["average_ask"] == pytest.approx(0.08)
    assert left_out["share_asking_under_10c"] == pytest.approx(0.5)
    assert left_out["book_curve_said"] == pytest.approx(0.03)


def test_tolerance_compares_the_gap_range_with_the_cost_of_trading():
    def tables(gap_low, gap_high):
        key = {"sample": study.BOTH, "scans": "all", "band": "20-50c"}
        bands = pd.DataFrame([{**key, "gap": 0.0, "gap_low": gap_low, "gap_high": gap_high}])
        costs = pd.DataFrame([{**key, "half_spread_cents": 2.0, "fee_cents": 1.0}])   # 3 points to trade
        return bands, costs

    inside = study.tolerance_table(*tables(-0.02, 0.025)).iloc[0]
    assert inside["trading_cost"] == pytest.approx(0.03)
    assert inside["range_inside_cost"]
    assert not study.tolerance_table(*tables(-0.04, 0.01)).iloc[0]["range_inside_cost"]


def test_rare_same_contracts_uses_only_contracts_both_sources_call_rare():
    rows = [contract("g1", 0.04, 0.98, "yes", books_over=0.02),   # both under 5%: counted
            contract("g1", 0.04, 0.98, "no", books_over=0.02),
            contract("g1", 0.04, 0.98, "no", books_over=0.08),    # only Kalshi under 5%
            contract("g1", 0.12, 0.90, "no", books_over=0.02)]    # only the book curve under 5%
    row = study.rare_same_contracts_table(prepared(rows), resamples=50).iloc[0]
    assert row["contracts"] == 2
    assert row["actual"] == pytest.approx(0.5)
    assert row["kalshi_expected"] == pytest.approx(0.03)
    assert row["book_expected"] == pytest.approx(0.02)
