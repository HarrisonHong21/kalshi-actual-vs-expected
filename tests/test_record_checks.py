"""Tests for record_checks.py on small made-up rows whose answers can be worked out by hand."""
import sys
from pathlib import Path
from statistics import NormalDist

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import record_checks as rc
import study

KICKOFF = pd.Timestamp("2026-10-11T17:00:00Z")


def prop(game, market_over, result, actual, line=10.5, stat="receptions", league="NFL", hours_before=1.0,
         model_over=None, underdog_line=None, underdog_over=None, prizepicks_line=None, prizepicks_over=None):
    return {
        "league": league, "event_id": game, "player": "A Player", "stat": stat, "line": line,
        "game_time": KICKOFF.isoformat(), "logged_at": (KICKOFF - pd.Timedelta(hours=hours_before)).isoformat(),
        "market_over": market_over, "model_over": model_over, "result": result, "actual": actual,
        "underdog_line": underdog_line, "underdog_market_over": underdog_over, "underdog_model_over": None,
        "prizepicks_line": prizepicks_line, "prizepicks_market_over": prizepicks_over, "prizepicks_model_over": None,
    }


def props_table(rows, tmp_path):
    df = pd.DataFrame(rows)
    paths = {}
    for league in ["NFL", "NBA"]:
        path = tmp_path / f"{league}.csv"
        df[df["league"] == league].drop(columns="league").to_csv(path, index=False)
        paths[league] = path
    return rc.load_props(paths)


def kalshi_row(game, yes_ask, no_ask, result, books_over, distance=0.5, stat="receptions", league="NFL",
               hours_before=1.0, settlement=None, yes_size=None, no_size=None, logged_day=None):
    logged = KICKOFF - pd.Timedelta(hours=hours_before)
    if logged_day:
        logged = pd.Timestamp(f"{logged_day}T16:00:00Z")
    return {
        "league": league, "event_id": game, "stat": stat, "distance": distance,
        "game_time": (logged + pd.Timedelta(hours=hours_before)).isoformat(), "logged_at": logged.isoformat(),
        "yes_ask": yes_ask, "no_ask": no_ask, "books_over": books_over, "books_under": 1 - books_over,
        "result": result, "settlement": settlement, "yes_ask_size": yes_size, "no_ask_size": no_size,
    }


def test_over_bias_is_hit_rate_minus_the_books_chance(tmp_path):
    rows = [prop("g1", 0.50, "over", 12), prop("g1", 0.50, "under", 9),
            prop("g2", 0.60, "under", 8), prop("g2", 0.60, "under", 7),
            prop("g2", 0.50, "push", 10.5), prop("g2", 0.50, "did not play", None)]   # both left out
    table = rc.over_bias_table(props_table(rows, tmp_path), resamples=50).set_index(["league", "stat"])
    row = table.loc[("NFL", "All stats")]
    assert row["props"] == 4
    assert row["actual_over"] == pytest.approx(0.25)
    assert row["books_over"] == pytest.approx(0.55)
    assert row["gap"] == pytest.approx(-0.30)


def test_props_logged_after_the_start_are_left_out(tmp_path):
    rows = [prop("g1", 0.50, "over", 12), prop("g1", 0.50, "over", 12, hours_before=-1)]
    assert len(props_table(rows, tmp_path)) == 1


def test_pickem_takes_the_side_the_books_favour_and_drops_pushes(tmp_path):
    rows = [
        prop("g1", 0.5, "over", 12, underdog_line=10.5, underdog_over=0.60),   # over favoured, hits
        prop("g1", 0.5, "under", 9, underdog_line=10.5, underdog_over=0.40),   # under favoured, hits
        prop("g2", 0.5, "over", 12, underdog_line=10.5, underdog_over=0.45),   # under favoured, misses
        prop("g2", 0.5, "over", 11, underdog_line=11.0, underdog_over=0.55),   # push: left out
    ]
    table = rc.pickem_table(props_table(rows, tmp_path), resamples=50).set_index(["league", "group"])
    every = table.loc[("NFL", "Every pick")]
    assert every["picks"] == 3
    assert every["hit"] == pytest.approx(2 / 3)
    assert every["expected"] == pytest.approx((0.60 + 0.60 + 0.55) / 3)
    rated = table.loc[("NFL", "Books rated 54%+")]
    assert rated["picks"] == 3


def test_prizepicks_receptions_below_the_books_line(tmp_path):
    rows = [prop("g1", 0.5, "over", 10, line=9.5, prizepicks_line=9.0, prizepicks_over=0.55),   # over hits
            prop("g1", 0.5, "under", 8, line=9.5, prizepicks_line=9.0, prizepicks_over=0.55),   # over misses
            prop("g2", 0.5, "under", 8, line=9.5, prizepicks_line=10.0, prizepicks_over=0.45)]  # line above: not in
    table = rc.pickem_table(props_table(rows, tmp_path), resamples=50).set_index("group")
    row = table.loc["PrizePicks whole-number receptions below the books' line, over favoured"]
    assert row["picks"] == 2
    assert row["hit"] == pytest.approx(0.5)
    assert row["expected"] == pytest.approx(0.55)


def test_model_brier_against_the_books(tmp_path):
    rows = [prop("g1", 0.6, "over", 12, model_over=0.8), prop("g2", 0.4, "under", 8, model_over=0.5)]
    row = rc.model_accuracy_table(props_table(rows, tmp_path), resamples=50).iloc[0]
    assert row["brier_model"] == pytest.approx((0.2 ** 2 + 0.5 ** 2) / 2)
    assert row["brier_books"] == pytest.approx((0.4 ** 2 + 0.4 ** 2) / 2)


def test_backtest_edge_rule_fee_and_part_way_settlement():
    # YES at 40c, books say 50%: cost 0.40 + 0.07 x 0.4 x 0.6 = 0.4168, edge 20%: bought.
    # NO at 61c, books say 50%: no edge.  A second contract settles part-way at 30c a YES.
    raw = pd.DataFrame([kalshi_row("g1", 0.40, 0.61, "yes", 0.50),
                        kalshi_row("g2", 0.40, 0.61, "scalar", 0.50, settlement=0.30),
                        kalshi_row("g3", 0.40, 0.61, "yes", 0.50, distance=1.5)])   # too far from the middle
    table = rc.backtest_table(raw, resamples=50).set_index(["league", "rule"])
    row = table.loc[("NFL", "Edge over 3%, within 1 swing, both sides")]
    cost = 0.40 + 0.07 * 0.40 * 0.60
    assert row["buys"] == 2
    assert row["return"] == pytest.approx((1.0 + 0.30 - 2 * cost) / (2 * cost))


def test_backtest_longshots_buy_every_cheap_yes():
    raw = pd.DataFrame([kalshi_row("g1", 0.10, 0.92, "no", 0.05), kalshi_row("g1", 0.15, 0.87, "yes", 0.10),
                        kalshi_row("g1", 0.20, 0.82, "yes", 0.15)])   # 20c: not a longshot
    row = rc.backtest_table(raw, resamples=50).set_index(["league", "rule"]).loc[("NFL", "Every YES at 15c or less")]
    cost = (0.10 + 0.07 * 0.10 * 0.90) + (0.15 + 0.07 * 0.15 * 0.85)
    assert row["buys"] == 2
    assert row["return"] == pytest.approx((1.0 - cost) / cost)


def test_paper_trial_counts_only_later_fresh_prices_and_caps_the_order():
    # NO at 40c with books giving the under 50%. Offered: 25 contracts, so the order is 10.
    # Fee on 10 at 40c: 16.8c, rounded up to 17c. Cost 4.17, pays 10 if the under wins.
    rows = [kalshi_row("g1", 0.61, 0.40, "no", 0.50, no_size=25, logged_day="2026-11-12"),
            kalshi_row("g2", 0.61, 0.40, "no", 0.50, no_size=25, logged_day="2026-11-01"),          # before the start date
            kalshi_row("g3", 0.61, 0.40, "no", 0.50, no_size=25, logged_day="2026-11-12", hours_before=8),  # not fresh
            kalshi_row("g4", 0.61, 0.40, "no", 0.50, logged_day="2026-11-12")]                       # no size saved
    table = rc.paper_trial_table(pd.DataFrame(rows), resamples=50).set_index("trial")
    row = table.loc["kalshi-edge-3"]
    assert row["buys"] == 1
    assert row["contracts"] == 10
    assert row["return"] == pytest.approx((10 - 4.17) / 4.17)
    assert pd.isna(row["return_low"])            # under 10 games: no range


def test_maker_trial_uses_the_maker_fee():
    rows = [kalshi_row("g1", 0.61, 0.40, "no", 0.50, no_size=10, logged_day="2026-11-24")]
    row = rc.paper_trial_table(pd.DataFrame(rows), resamples=50).set_index("trial").loc["kalshi-nfl-unders-maker"]
    cost = 4.0 + 0.05    # 0.0175 x 0.4 x 0.6 x 10 = 4.2c, rounded up to 5c
    assert row["return"] == pytest.approx((10 - cost) / cost)


def test_lognormal_curve_puts_half_above_the_median_and_more_in_the_right_tail():
    assert rc._lognormal_over(60, 60, 25) == pytest.approx(0.5)
    assert rc._lognormal_over(110, 60, 25) > 1 - NormalDist(60, 25).cdf(110)


def test_on_book_curve_keeps_one_version():
    df = pd.DataFrame({"model_version": [study.BOOK_CURVE_VERSION, "2026-11-23.1"], "x": [1, 2]})
    assert study.on_book_curve(df)["x"].tolist() == [1]
    assert len(study.on_book_curve(df.drop(columns="model_version"))) == 2
