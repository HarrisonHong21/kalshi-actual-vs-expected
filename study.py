"""Actual versus expected for Kalshi player-prop prices.

Run:  .venv/bin/python study.py PATH/kalshi_record.csv PATH/nba_kalshi_record.csv

Each row of the input is one Kalshi contract ("will this player go over this line?") with the
price to buy YES and the price to buy NO just before the game, and how it settled. The script:

1. compares how often contracts paid out (actual) with the chance their price implied (expected);
2. splits what a buyer lost into the spread, the fee and the midpoint being wrong;
3. compares Kalshi's midpoint with a second opinion: a curve fitted to sportsbook lines;
4. checks how much the headline results move when any one game is dropped, and how the
   return changes with the size of the order.

Every definition below was fixed before looking at the results, so the study can't go hunting
for a flattering cut of the data.
"""
import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw straight to a file; no window needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Price bands, by the midpoint between the buy and sell price. A band includes its lower edge
# and excludes its upper edge, so a 5-cent midpoint is in "5-10c".
BAND_EDGES = [0.0, 0.05, 0.10, 0.20, 0.50, 0.80, 1.0001]
BAND_LABELS = ["under 5c", "5-10c", "10-20c", "20-50c", "50-80c", "80c and up"]
ALL_PRICES = "all prices"

# How far a contract's line is from the middle of the sportsbooks' lines for that player,
# counted in "swings" (the typical game-to-game spread of the stat). A line 2+ swings above
# the middle is a rare outcome. A band includes its lower edge.
DISTANCE_EDGES = [-math.inf, -1, 0, 1, 2, math.inf]
DISTANCE_LABELS = ["1+ below", "0-1 below", "0-1 above", "1-2 above", "2+ above"]

RARE_BELOW = 0.05    # "rare" = given under a 5% chance

# Classical "full credibility" standard: 1,082 expected events gives a 90% chance that the
# observed rate is within 5% of the true rate.
FULL_CREDIBILITY = 1082

FRESH_HOURS = 6      # a scan this close to the start of the game counts as "fresh"
FEE_RATE = 0.07      # Kalshi's fee per contract is 0.07 x price x (1 - price), rounded up per order
ORDER_SIZE = 10      # contracts per order when working out the fee
RESAMPLES = 2000
SEED = 20261110

KALSHI, BOOK = "Kalshi midpoint", "book curve"
BOTH = "NFL + NBA"
AUTHOR = "Harrison Hong"

# Chart colors. Each thing keeps the same color in every chart.
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
LEAGUE_COLORS = {"NFL": "#2a78d6", "NBA": "#eb6834"}
SPREAD_COLOR, FEE_COLOR = "#1baf7a", "#eda100"
SOURCE_STYLE = {KALSHI: ("#6250d6", "o"), BOOK: ("#52514e", "s")}  # color, marker shape


def load(paths):
    """Read one CSV per league ({"NFL": path, "NBA": path}) into a single table."""
    tables = []
    for league, path in paths.items():
        table = pd.read_csv(path)
        table["league"] = league
        tables.append(table)
    return pd.concat(tables, ignore_index=True)


def prepare(raw):
    """Keep contracts that settled plainly yes or no, and add the columns the study uses.

    Contracts settled part-way ("scalar", e.g. the player left the game) are left out: they
    didn't simply happen or not happen.
    """
    df = raw[raw["result"].isin(["yes", "no"])].copy()
    df["happened"] = (df["result"] == "yes").astype(int)

    # Buying NO at no_ask is the same as selling YES at 1 - no_ask, so that is the YES bid.
    # Prices are whole cents, so the midpoint is a whole half-cent: rounding removes float
    # noise (0.04999999 instead of 0.05) that would put a contract in the wrong band.
    df["midpoint"] = ((df["yes_ask"] + (1 - df["no_ask"])) / 2).round(4)
    df["band"] = pd.cut(df["midpoint"], BAND_EDGES, right=False, labels=BAND_LABELS)

    # The data's `distance` has no sign, so add one: negative when the line is below the middle.
    signed_distance = np.where(df["line"] > df["middle"], 1, -1) * df["distance"]
    df["distance_band"] = pd.cut(signed_distance, DISTANCE_EDGES, right=False, labels=DISTANCE_LABELS)

    kickoff = pd.to_datetime(df["game_time"], utc=True)
    scanned = pd.to_datetime(df["logged_at"], utc=True)
    df["hours_before"] = (kickoff - scanned).dt.total_seconds() / 3600
    df["fresh"] = df["hours_before"] <= FRESH_HOURS

    df["game_id"] = df["league"] + ":" + df["event_id"].astype(str)
    return df


def credibility(contracts, expected_rate):
    """Share of full credibility (0 to 1) for a yes/no rate, if every contract were independent.

    The 1,082 standard counts events. For a yes/no outcome with chance p, the same 90%/5% test
    needs 1,082 x (1 - p) / p contracts; partial credibility is the square root of the share.
    """
    if contracts == 0 or expected_rate <= 0:
        return 0.0
    needed = FULL_CREDIBILITY * (1 - expected_rate) / expected_rate
    return min(1.0, math.sqrt(contracts / needed))


def _per_game(df, games, column, group_column="band", labels=BAND_LABELS):
    """The sum of `column` for each game (rows) and each group of contracts (columns)."""
    grid = df.pivot_table(index="game_id", columns=group_column, values=column, aggfunc="sum", observed=False)
    return grid.reindex(index=games, columns=labels).fillna(0).to_numpy(dtype=float)


def _game_draws(n_games, resamples, seed):
    """Resampling by game. A player has a whole ladder of contracts in one game, and they win
    or lose together, so the contracts are not independent. To be honest about that, every
    range in the study comes from redrawing the list of games (with repeats) many times and
    keeping each game's contracts together. Row r of the result says how many times each game
    was picked in resample r."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_games, np.full(n_games, 1 / n_games), size=resamples)


def calibration(df, expected_column, group_column, labels, resamples=RESAMPLES, seed=SEED, games=None):
    """Actual versus expected for each group of contracts, with a 95% range on the gap.

    `expected_column` holds the chance each contract was given (a price, or a model's chance);
    `group_column` says which group each contract is in, and `labels` lists the groups in order.
    `games` is the full list of games to resample from, when df is only a slice of a sample.
    """
    if df.empty:
        return pd.DataFrame()
    df = df.assign(one=1)
    if games is None:
        games = sorted(df["game_id"].unique())
    count = _per_game(df, games, "one", group_column, labels)
    hits = _per_game(df, games, "happened", group_column, labels)
    expected = _per_game(df, games, expected_column, group_column, labels)

    draws = _game_draws(len(games), resamples, seed)
    with np.errstate(invalid="ignore", divide="ignore"):
        resampled_gap = (draws @ hits - draws @ expected) / (draws @ count)

    rows = []
    for i, label in enumerate(labels):
        n = int(count[:, i].sum())
        if n == 0:
            continue
        low, high = np.nanpercentile(resampled_gap[:, i], [2.5, 97.5])
        expected_hits = expected[:, i].sum()
        rows.append({
            group_column: label,
            "contracts": n,
            "games": int((count[:, i] > 0).sum()),
            "yes_outcomes": int(hits[:, i].sum()),
            "actual": hits[:, i].sum() / n,
            "expected": expected_hits / n,
            "gap": (hits[:, i].sum() - expected_hits) / n,
            "gap_low": low,
            "gap_high": high,
            "actual_to_expected": hits[:, i].sum() / expected_hits if expected_hits > 0 else float("nan"),
            "gap_sd": np.nanstd(resampled_gap[:, i]),
        })
    return pd.DataFrame(rows)


def band_table(df, resamples=RESAMPLES, seed=SEED):
    """Actual versus Kalshi's midpoint for each price band, with the credibility of each band."""
    table = calibration(df[df["midpoint"].notna()], "midpoint", "band", BAND_LABELS, resamples, seed)
    if table.empty:
        return table

    widening, if_independent, by_game = [], [], []
    for row in table.itertuples():
        # How much wider the by-game range is than it would be if contracts were independent.
        independent_sd = math.sqrt(row.actual * (1 - row.actual) / row.contracts)
        wider = row.gap_sd / independent_sd if independent_sd > 0 else float("nan")
        # Clustering shrinks the sample that really counts by the square of that widening.
        effective_n = row.contracts / wider ** 2 if wider and wider > 1 else row.contracts
        widening.append(wider)
        if_independent.append(credibility(row.contracts, row.expected))
        by_game.append(credibility(effective_n, row.expected))

    table = table.drop(columns="gap_sd")
    table["widening"] = widening
    table["credibility_if_independent"] = if_independent
    table["credibility_by_game"] = by_game
    return table


def fee_per_contract(price, contracts=ORDER_SIZE):
    """Kalshi's fee per contract on one order. The fee on the whole order is rounded up to the
    next cent, so small orders pay more per contract: one 20-cent contract pays 2 cents, not 1.12."""
    order_fee = math.ceil(round(FEE_RATE * price * (1 - price) * contracts * 100, 6)) / 100
    return order_fee / contracts


def yes_buy_return(df, contracts=ORDER_SIZE):
    """Return per dollar spent from buying YES on every contract in df at the ask, fee included
    (total profit / total spent: the mirror image of an insurer's loss ratio)."""
    spent = df["yes_ask"] + df["yes_ask"].map(lambda p: fee_per_contract(p, contracts))
    return (df["happened"].sum() - spent.sum()) / spent.sum()


def cost_table(df, contracts=ORDER_SIZE, resamples=RESAMPLES, seed=SEED):
    """Where a YES buyer's money went, for each price band and for all prices together.

    A buyer pays the ask plus the fee and gets $1 if the contract pays out. What they lose per
    contract splits exactly into three parts:

        (ask + fee) - payout  =  (ask - midpoint)  +  fee  +  (midpoint - payout)
                                  the spread          fee     the midpoint being wrong

    Each part is shown as a share of the money spent. The first two are known when you buy;
    only the third depends on what happened, so only it (and the total) gets a range.
    """
    df = df[df["midpoint"].notna()].assign(one=1)
    df["spread"] = df["yes_ask"] - df["midpoint"]
    df["fee"] = df["yes_ask"].map(lambda p: fee_per_contract(p, contracts))
    df["miss"] = df["midpoint"] - df["happened"]
    df["spent"] = df["yes_ask"] + df["fee"]

    games = sorted(df["game_id"].unique())
    sums = {}
    for column in ["one", "yes_ask", "midpoint", "happened", "spread", "fee", "miss", "spent"]:
        by_band = _per_game(df, games, column)
        # one extra column on the end for all prices together
        sums[column] = np.column_stack([by_band, by_band.sum(axis=1)])

    draws = _game_draws(len(games), resamples, seed)
    with np.errstate(invalid="ignore", divide="ignore"):
        resampled_spent = draws @ sums["spent"]
        resampled_miss = (draws @ sums["miss"]) / resampled_spent
        resampled_lost = (draws @ (sums["spread"] + sums["fee"] + sums["miss"])) / resampled_spent

    rows = []
    for i, label in enumerate(BAND_LABELS + [ALL_PRICES]):
        total = {column: grid[:, i].sum() for column, grid in sums.items()}
        n = int(total["one"])
        if n == 0:
            continue
        spent = total["spent"]
        miss_low, miss_high = np.nanpercentile(resampled_miss[:, i], [2.5, 97.5])
        lost_low, lost_high = np.nanpercentile(resampled_lost[:, i], [2.5, 97.5])
        lost = (total["spread"] + total["fee"] + total["miss"]) / spent
        rows.append({
            "band": label,
            "contracts": n,
            "ask_cents": total["yes_ask"] / n * 100,
            "midpoint_cents": total["midpoint"] / n * 100,
            "spread_cents": total["spread"] / n * 100,
            "fee_cents": total["fee"] / n * 100,
            "midpoint_miss_cents": total["miss"] / n * 100,
            "lost_to_spread": total["spread"] / spent,
            "lost_to_fee": total["fee"] / spent,
            "lost_to_midpoint": total["miss"] / spent,
            "lost_to_midpoint_low": miss_low,
            "lost_to_midpoint_high": miss_high,
            "return": -lost,
            "return_low": -lost_high,
            "return_high": -lost_low,
            "return_before_fee": (total["happened"] - total["yes_ask"]) / total["yes_ask"],
        })
    return pd.DataFrame(rows)


def accuracy_table(df, resamples=RESAMPLES, seed=SEED):
    """Overall accuracy of Kalshi's midpoint and of the book curve, as Brier scores.

    A Brier score is the average of (chance given - what happened) squared: 0 is perfect, and
    lower is better. The "base rate" score is what you'd get by giving every contract the same
    chance (the overall share that paid out), so it shows how much either source adds.
    """
    df = df[df["midpoint"].notna()]
    if df.empty:
        return pd.DataFrame()
    errors = pd.DataFrame({
        "game_id": df["game_id"],
        "one": 1,
        "kalshi": (df["midpoint"] - df["happened"]) ** 2,
        "book": (df["books_over"] - df["happened"]) ** 2,
    }).groupby("game_id").sum()

    draws = _game_draws(len(errors), resamples, seed)
    resampled = (draws @ (errors["kalshi"] - errors["book"]).to_numpy()) / (draws @ errors["one"].to_numpy())
    low, high = np.percentile(resampled, [2.5, 97.5])
    n = len(df)
    base_rate = df["happened"].mean()
    return pd.DataFrame([{
        "contracts": n,
        "games": len(errors),
        "brier_kalshi": errors["kalshi"].sum() / n,
        "brier_book": errors["book"].sum() / n,
        "brier_base_rate": base_rate * (1 - base_rate),
        "kalshi_minus_book": (errors["kalshi"].sum() - errors["book"].sum()) / n,
        "kalshi_minus_book_low": low,
        "kalshi_minus_book_high": high,
    }])


def rare_table(df, resamples=RESAMPLES, seed=SEED):
    """Rare outcomes: for each source, the contracts it gave under a 5% chance, and how often
    they paid out. Each source is judged on its own set of contracts."""
    df = df[df["midpoint"].notna()]
    games = sorted(df["game_id"].unique())  # resample from every game, not only those with a rare contract
    tables = []
    for source, column in [(KALSHI, "midpoint"), (BOOK, "books_over")]:
        rare = df[df[column] < RARE_BELOW].assign(source=source)
        tables.append(calibration(rare, column, "source", [source], resamples, seed, games))
    return pd.concat(tables, ignore_index=True).drop(columns="gap_sd", errors="ignore")


def distance_table(df, resamples=RESAMPLES, seed=SEED):
    """Both sources on the same contracts, grouped by how far the line is from the books' middle."""
    df = df[df["midpoint"].notna()]
    kalshi = calibration(df, "midpoint", "distance_band", DISTANCE_LABELS, resamples, seed)
    book = calibration(df, "books_over", "distance_band", DISTANCE_LABELS, resamples, seed)
    if kalshi.empty:
        return kalshi
    table = kalshi[["distance_band", "contracts", "games", "yes_outcomes", "actual"]].copy()
    for name, source in [("kalshi", kalshi), ("book", book)]:
        table[f"{name}_expected"] = source["expected"]
        table[f"{name}_gap"] = source["gap"]
        table[f"{name}_gap_low"] = source["gap_low"]
        table[f"{name}_gap_high"] = source["gap_high"]
    return table


def sensitivity_table(df, contracts=ORDER_SIZE):
    """Leave-one-game-out check: each headline result worked out again with every game dropped
    in turn, to see whether one game drives it. Shows the lowest and highest value that gives.

    Every result here is a total divided by a total (profit / money spent, or summed gaps /
    contracts), so dropping a game just takes its part out of both.
    """
    df = df[df["midpoint"].notna()].assign(one=1.0)
    df["spent"] = df["yes_ask"] + df["yes_ask"].map(lambda p: fee_per_contract(p, contracts))
    df["profit"] = df["happened"] - df["spent"]
    df["miss"] = df["midpoint"] - df["happened"]
    df["kalshi_gap"] = df["happened"] - df["midpoint"]
    df["book_gap"] = df["happened"] - df["books_over"]
    df["brier_gap"] = (df["midpoint"] - df["happened"]) ** 2 - (df["books_over"] - df["happened"]) ** 2
    under5 = df["band"] == "under 5c"

    results = [
        ("return_all", "Return from buying YES, all prices, both leagues", "%", df, "profit", "spent"),
        ("return_under5", "Return from buying YES under 5c, both leagues", "%", df[under5], "profit", "spent"),
        ("midpoint_all", "Midpoint error as a share of money spent, both leagues", "%", df, "miss", "spent"),
        ("nba_under5_gap", "NBA under 5c: actual minus expected", "points",
         df[under5 & (df["league"] == "NBA")], "kalshi_gap", "one"),
        ("nfl_book_rare_gap", "NFL contracts the book curve gave under 5%: actual minus expected", "points",
         df[(df["books_over"] < RARE_BELOW) & (df["league"] == "NFL")], "book_gap", "one"),
        ("brier_gap", "Brier score, Kalshi minus book curve, both leagues", "Brier", df, "brier_gap", "one"),
    ]
    rows = []
    for key, name, unit, part, top, bottom in results:
        if part.empty:
            continue
        per_game = part.groupby("game_id")[[top, bottom]].sum()
        without_one = (per_game[top].sum() - per_game[top]) / (per_game[bottom].sum() - per_game[bottom])
        rows.append({
            "key": key,
            "result": name,
            "unit": unit,
            "games": len(per_game),
            "full_sample": per_game[top].sum() / per_game[bottom].sum(),
            "lowest_without_one_game": without_one.min(),
            "highest_without_one_game": without_one.max(),
        })
    return pd.DataFrame(rows)


def order_size_table(df, sizes=(100, 10, 1)):
    """The return from buying YES at different order sizes. Kalshi rounds the fee up to the next
    cent on the whole order, so the fee per contract is higher on small orders."""
    df = df[df["midpoint"].notna()]
    rows = []
    for label in BAND_LABELS + [ALL_PRICES]:
        part = df if label == ALL_PRICES else df[df["band"] == label]
        if part.empty:
            continue
        row = {"band": label, "no_fee": (part["happened"].sum() - part["yes_ask"].sum()) / part["yes_ask"].sum()}
        for size in sizes:
            row[f"contracts_{size}"] = yes_buy_return(part, size)
        rows.append(row)
    return pd.DataFrame(rows)


def all_tables(df, make_table=band_table, early=False):
    """One table for each league and for both together, on all scans and on fresh scans only
    (and, if `early`, on scans made more than FRESH_HOURS before the start)."""
    scan_sets = [("all", df), ("fresh", df[df["fresh"]])]
    if early:
        scan_sets.append(("early", df[~df["fresh"]]))
    tables = []
    for scans, subset in scan_sets:
        for sample, part in [(BOTH, subset), ("NFL", subset[subset["league"] == "NFL"]),
                             ("NBA", subset[subset["league"] == "NBA"])]:
            table = make_table(part)
            table.insert(0, "scans", scans)
            table.insert(0, "sample", sample)
            tables.append(table)
    return pd.concat(tables, ignore_index=True)


def _range(low, high, scale=100, digits=1):
    return [f"{lo * scale:+.{digits}f} to {hi * scale:+.{digits}f}" for lo, hi in zip(low, high)]


def show(table):
    """A band table as readable text: rates in %, gaps in percentage points."""
    out = pd.DataFrame({
        "band": table["band"],
        "contracts": table["contracts"],
        "games": table["games"],
        "yes": table["yes_outcomes"],
        "actual %": (table["actual"] * 100).round(1),
        "expected %": (table["expected"] * 100).round(1),
        "gap pts": (table["gap"] * 100).round(1),
        "95% range": _range(table["gap_low"], table["gap_high"]),
        "A/E": table["actual_to_expected"].round(2),
        "widening": table["widening"].round(1),
        "cred. indep.": table["credibility_if_independent"].round(2),
        "cred. by game": table["credibility_by_game"].round(2),
    })
    return out.to_string(index=False)


def show_cost(table):
    """A cost table as readable text: prices in cents, losses and returns in % of money spent."""
    out = pd.DataFrame({
        "band": table["band"],
        "contracts": table["contracts"],
        "ask c": table["ask_cents"].round(1),
        "spread c": table["spread_cents"].round(2),
        "fee c": table["fee_cents"].round(2),
        "spread %": (table["lost_to_spread"] * 100).round(1),
        "fee %": (table["lost_to_fee"] * 100).round(1),
        "midpoint %": (table["lost_to_midpoint"] * 100).round(1),
        "midpoint range": _range(table["lost_to_midpoint_low"], table["lost_to_midpoint_high"]),
        "return %": (table["return"] * 100).round(1),
        "return range": _range(table["return_low"], table["return_high"]),
        "before fee %": (table["return_before_fee"] * 100).round(1),
    })
    return out.to_string(index=False)


def show_accuracy(table):
    out = pd.DataFrame({
        "sample": table["sample"],
        "scans": table["scans"],
        "contracts": table["contracts"],
        "games": table["games"],
        "Kalshi": table["brier_kalshi"].round(4),
        "book curve": table["brier_book"].round(4),
        "base rate": table["brier_base_rate"].round(4),
        "Kalshi - book": table["kalshi_minus_book"].round(4),
        "95% range": _range(table["kalshi_minus_book_low"], table["kalshi_minus_book_high"], scale=1, digits=4),
    })
    return out.to_string(index=False)


def show_rare(table):
    out = pd.DataFrame({
        "source": table["source"],
        "contracts": table["contracts"],
        "games": table["games"],
        "yes": table["yes_outcomes"],
        "actual %": (table["actual"] * 100).round(1),
        "expected %": (table["expected"] * 100).round(1),
        "gap pts": (table["gap"] * 100).round(1),
        "95% range": _range(table["gap_low"], table["gap_high"]),
        "A/E": table["actual_to_expected"].round(2),
    })
    return out.to_string(index=False)


def show_distance(table):
    out = pd.DataFrame({
        "line vs middle": table["distance_band"],
        "contracts": table["contracts"],
        "games": table["games"],
        "yes": table["yes_outcomes"],
        "actual %": (table["actual"] * 100).round(1),
        "Kalshi %": (table["kalshi_expected"] * 100).round(1),
        "Kalshi gap": (table["kalshi_gap"] * 100).round(1),
        "Kalshi range": _range(table["kalshi_gap_low"], table["kalshi_gap_high"]),
        "book %": (table["book_expected"] * 100).round(1),
        "book gap": (table["book_gap"] * 100).round(1),
        "book range": _range(table["book_gap_low"], table["book_gap_high"]),
    })
    return out.to_string(index=False)


def _row(table, sample, scans="all", **match):
    """The one row of a table for this sample and scan set (and any other column values)."""
    picked = table[(table["sample"] == sample) & (table["scans"] == scans)]
    for column, value in match.items():
        picked = picked[picked[column] == value]
    return picked.iloc[0]


def _pct(x, signed=False):
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def _signed(x, digits=1):
    """A number with its sign, except that a rounded zero is shown as plain 0.0 (not -0.0)."""
    return f"{0:.{digits}f}" if round(x, digits) == 0 else f"{x:+.{digits}f}"


def _span(low, high, unit=""):
    return f"{low * 100:+.1f}{unit} to {high * 100:+.1f}{unit}"


def key_numbers(raw, df, bands, costs, accuracy, rare, distance, sensitivity, order_sizes):
    """The headline figures as text, on all scans. The one-page PDF is filled from these and a
    test checks that the README quotes every one, so no number in either is typed by hand."""
    kickoff = pd.to_datetime(df["game_time"], utc=True).dt.tz_convert("America/New_York")
    first, last = kickoff.min(), kickoff.max()
    pooled = bands[(bands["sample"] == BOTH) & (bands["scans"] == "all")]
    leagues = bands[(bands["sample"] != BOTH) & (bands["scans"] == "all")]
    nfl_longshots = leagues[(leagues["sample"] == "NFL") & leagues["band"].isin(BAND_LABELS[:3])]

    under5, five_to_ten = _row(bands, BOTH, band="under 5c"), _row(bands, BOTH, band="5-10c")
    nba_under5 = _row(bands, "NBA", band="under 5c")
    cost_all, cost_under5 = _row(costs, BOTH, band=ALL_PRICES), _row(costs, BOTH, band="under 5c")
    cost_80 = _row(costs, BOTH, band="80c and up")
    nfl_cost_under5, nba_cost_under5 = _row(costs, "NFL", band="under 5c"), _row(costs, "NBA", band="under 5c")
    brier = _row(accuracy, BOTH)
    nfl_book, nfl_kalshi = _row(rare, "NFL", source=BOOK), _row(rare, "NFL", source=KALSHI)
    nba_book = _row(rare, "NBA", source=BOOK)
    nfl_far = _row(distance, "NFL", distance_band="2+ above")
    with_midpoint = df[df["midpoint"].notna()]
    median_hours = with_midpoint.groupby("league")["hours_before"].median()
    dropped = sensitivity.set_index("key")
    sizes_under5 = order_sizes.set_index("band").loc["under 5c"]

    def without_one_game(key, unit=""):
        return _span(dropped.loc[key, "lowest_without_one_game"], dropped.loc[key, "highest_without_one_game"], unit)

    return {
        "rows_read": f"{len(raw):,}",
        "left_out_part_way": f"{len(raw) - len(df):,}",
        "contracts": f"{int(cost_all['contracts']):,}",
        "games": str(df["game_id"].nunique()),
        "nfl_games": str(df[df["league"] == "NFL"]["game_id"].nunique()),
        "nba_games": str(df[df["league"] == "NBA"]["game_id"].nunique()),
        "player_games": f"{df.groupby(['game_id', 'player']).ngroups:,}",
        "period": f"{first.day} {first:%B} to {last.day} {last:%B %Y}",
        "nfl_median_hours_before": f"{median_hours['NFL']:.1f}",
        "nba_median_hours_before": f"{median_hours['NBA']:.1f}",
        "fresh_share": f"{with_midpoint['fresh'].mean() * 100:.0f}%",
        "longest_hours_before": f"{with_midpoint['hours_before'].max():.0f}",

        "all_pooled_ranges_include_zero": bool(((pooled["gap_low"] <= 0) & (pooled["gap_high"] >= 0)).all()),
        "pooled_gap_lowest": f"{pooled['gap'].min() * 100:+.1f}",
        "pooled_gap_highest": f"{pooled['gap'].max() * 100:+.1f}",
        # the largest gap, in either direction, that any band's range still allows
        "pooled_widest_bound": f"{max(pooled['gap_low'].abs().max(), pooled['gap_high'].abs().max()) * 100:.1f}",
        "under5_expected": _pct(under5["expected"]),
        "under5_plausible_low": _pct(under5["expected"] + under5["gap_low"]),
        "under5_plausible_high": _pct(under5["expected"] + under5["gap_high"]),
        "nfl_longshot_largest_gap": f"{nfl_longshots['gap'].abs().max() * 100:.1f}",
        "nba_under5_actual": _pct(nba_under5["actual"]),
        "nba_under5_expected": _pct(nba_under5["expected"]),
        "nba_under5_payouts": str(int(nba_under5["yes_outcomes"])),
        "nba_under5_range": _span(nba_under5["gap_low"], nba_under5["gap_high"]),
        "credibility_under5": f"{under5['credibility_if_independent']:.2f}",
        "credibility_under5_by_game": f"{under5['credibility_by_game']:.2f}",
        "credibility_5to10": f"{five_to_ten['credibility_if_independent']:.2f}",
        "credibility_5to10_by_game": f"{five_to_ten['credibility_by_game']:.2f}",
        "widening_lowest": f"{leagues['widening'].min():.1f}",
        "widening_highest": f"{leagues['widening'].max():.1f}",

        "return_all": _pct(cost_all["return"], signed=True),
        "return_all_range": _span(cost_all["return_low"], cost_all["return_high"], "%"),
        "spread_all": _pct(cost_all["lost_to_spread"]),
        "fee_all": _pct(cost_all["lost_to_fee"]),
        "midpoint_all": _pct(cost_all["lost_to_midpoint"]),
        "midpoint_all_range": _span(cost_all["lost_to_midpoint_low"], cost_all["lost_to_midpoint_high"], "%"),
        "known_share_of_loss": f"{(cost_all['lost_to_spread'] + cost_all['lost_to_fee']) / -cost_all['return'] * 100:.0f}%",
        "return_under5": _pct(cost_under5["return"], signed=True),
        "spread_under5": _pct(cost_under5["lost_to_spread"]),
        "return_80_up": _pct(cost_80["return"], signed=True),
        "nfl_midpoint_under5": _pct(nfl_cost_under5["lost_to_midpoint"], signed=True),
        "nba_spread_under5": _pct(nba_cost_under5["lost_to_spread"]),
        "nba_midpoint_under5": _pct(nba_cost_under5["lost_to_midpoint"]),
        "nfl_spread_cents": f"{_row(costs, 'NFL', band=ALL_PRICES)['spread_cents']:.1f}",
        "nba_spread_cents": f"{_row(costs, 'NBA', band=ALL_PRICES)['spread_cents']:.1f}",

        "return_all_without_one_game": without_one_game("return_all", "%"),
        "return_under5_without_one_game": without_one_game("return_under5", "%"),
        "nba_under5_gap_without_one_game": without_one_game("nba_under5_gap"),
        "nfl_book_rare_gap_without_one_game": without_one_game("nfl_book_rare_gap"),
        "return_under5_no_fee": _pct(sizes_under5["no_fee"], signed=True),
        "return_under5_100_contracts": _pct(sizes_under5["contracts_100"], signed=True),
        "return_under5_1_contract": _pct(sizes_under5["contracts_1"], signed=True),

        "brier_kalshi": f"{brier['brier_kalshi']:.4f}",
        "brier_book": f"{brier['brier_book']:.4f}",
        "brier_base_rate": f"{brier['brier_base_rate']:.4f}",
        "brier_difference_range": f"{brier['kalshi_minus_book_low']:+.4f} to {brier['kalshi_minus_book_high']:+.4f}",
        "nfl_book_rare_actual": _pct(nfl_book["actual"]),
        "nfl_book_rare_expected": _pct(nfl_book["expected"]),
        "nfl_kalshi_rare_actual": _pct(nfl_kalshi["actual"]),
        "nfl_kalshi_rare_expected": _pct(nfl_kalshi["expected"]),
        "nba_book_rare_actual": _pct(nba_book["actual"]),
        "nba_book_rare_expected": _pct(nba_book["expected"]),
        "nfl_far_actual": _pct(nfl_far["actual"]),
        "nfl_far_kalshi": _pct(nfl_far["kalshi_expected"]),
        "nfl_far_book": _pct(nfl_far["book_expected"]),
    }


def _md(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def markdown_tables(bands, costs, accuracy, rare, distance, sensitivity, order_sizes):
    """The tables shown in the README, as Markdown, so they are copied and never retyped."""
    def gap(row, name="gap"):
        return f"{_signed(row[name] * 100)} ({_span(row[name + '_low'], row[name + '_high'])})"

    def money(row, name):
        return f"{_pct(row[name], signed=True)} ({_span(row[name + '_low'], row[name + '_high'], '%')})"

    pooled_bands = [_row(bands, BOTH, band=b) for b in BAND_LABELS]
    pooled_costs = [_row(costs, BOTH, band=b) for b in BAND_LABELS + [ALL_PRICES]]
    scan_names = {"all": "all", "fresh": f"within {FRESH_HOURS} hours of the start", "early": f"more than {FRESH_HOURS} hours before"}

    tables = {
        "Actual versus expected, both leagues": _md(
            ["Price band", "Contracts", "Paid out", "Expected", "Gap in points (95% range)",
             "Credibility if independent", "Credibility by game"],
            [[r["band"], f"{r['contracts']:,}", _pct(r["actual"]), _pct(r["expected"]), gap(r),
              f"{r['credibility_if_independent']:.2f}", f"{r['credibility_by_game']:.2f}"] for r in pooled_bands]),
        "Gap by league": _md(
            ["Price band", "NFL gap in points (95% range)", "NBA gap in points (95% range)"],
            [[b, gap(_row(bands, "NFL", band=b)), gap(_row(bands, "NBA", band=b))] for b in BAND_LABELS]),
        "Where a YES buyer's money went, both leagues": _md(
            ["Price band", "Average ask", "Spread", "Fee", "Midpoint error (95% range)", "Return (95% range)"],
            [[r["band"], f"{r['ask_cents']:.1f}c", _pct(r["lost_to_spread"]), _pct(r["lost_to_fee"]),
              money(r, "lost_to_midpoint"), money(r, "return")] for r in pooled_costs]),
        "Longshot cost by league": _md(
            ["League", "Price band", "Spread", "Fee", "Midpoint error (95% range)", "Return (95% range)"],
            [[league, r["band"], _pct(r["lost_to_spread"]), _pct(r["lost_to_fee"]), money(r, "lost_to_midpoint"),
              money(r, "return")] for league in ["NFL", "NBA"]
             for r in [_row(costs, league, band=b) for b in BAND_LABELS[:2]]]),
        "Overall accuracy (Brier score)": _md(
            ["Sample", "Scans", "Contracts", "Games", "Kalshi midpoint", "Book curve", "Same chance for all",
             "Kalshi minus book (95% range)"],
            [[r["sample"], scan_names[r["scans"]], f"{r['contracts']:,}", r["games"], f"{r['brier_kalshi']:.4f}",
              f"{r['brier_book']:.4f}", f"{r['brier_base_rate']:.4f}",
              f"{_signed(r['kalshi_minus_book'], 4)} "
              f"({r['kalshi_minus_book_low']:+.4f} to {r['kalshi_minus_book_high']:+.4f})"]
             for _, r in accuracy.iterrows()]),
        "Rare outcomes": _md(
            ["League", "Source", "Contracts", "Payouts", "Paid out", "Expected", "Gap in points (95% range)"],
            [[league, r["source"], f"{r['contracts']:,}", r["yes_outcomes"], _pct(r["actual"]), _pct(r["expected"]), gap(r)]
             for league in ["NFL", "NBA"] for r in [_row(rare, league, source=s) for s in [KALSHI, BOOK]]]),
        "Kalshi versus the book curve by distance": _md(
            ["League", "Line against the books' middle", "Contracts", "Paid out", "Kalshi midpoint said",
             "Book curve said", "Kalshi gap in points (95% range)", "Book curve gap in points (95% range)"],
            [[league, r["distance_band"], f"{r['contracts']:,}", _pct(r["actual"]), _pct(r["kalshi_expected"]),
              _pct(r["book_expected"]), gap(r, "kalshi_gap"), gap(r, "book_gap")]
             for league in ["NFL", "NBA"]
             for _, r in distance[(distance["sample"] == league) & (distance["scans"] == "all")].iterrows()]),
    }
    def in_unit(x, unit):
        return {"%": _pct(x, signed=True), "points": _signed(x * 100), "Brier": _signed(x, 4)}[unit]

    tables["One game dropped at a time"] = _md(
        ["Result", "Games", "Full sample", "Lowest with one game dropped", "Highest with one game dropped"],
        [[r["result"] + (f" ({r['unit']})" if r["unit"] != "%" else ""), r["games"], in_unit(r["full_sample"], r["unit"]),
          in_unit(r["lowest_without_one_game"], r["unit"]), in_unit(r["highest_without_one_game"], r["unit"])]
         for _, r in sensitivity.iterrows()])
    tables["Return from buying YES by order size"] = _md(
        ["Price band", "No fee", "100 contracts", "10 contracts", "1 contract"],
        [[r["band"], _pct(r["no_fee"], signed=True), _pct(r["contracts_100"], signed=True),
          _pct(r["contracts_10"], signed=True), _pct(r["contracts_1"], signed=True)] for _, r in order_sizes.iterrows()])
    return "\n\n".join(f"## {title}\n\n{table}" for title, table in tables.items()) + "\n"


def write_summary_pdf(n, charts, path):
    """The one-page summary. `n` is the dict from key_numbers; `charts` is the charts folder."""
    title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=INK)
    byline = ParagraphStyle("byline", fontName="Helvetica", fontSize=9, leading=12, textColor=MUTED)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10.5, textColor=MUTED)

    def finding(heading, text, chart, aspect):
        width = 4.4 * inch
        words = Paragraph(f"<b>{heading}</b><br/>{text}", body)
        return [words, Image(str(Path(charts) / chart), width=width, height=width / aspect)]

    findings = Table([
        finding("1. No measurable gap between prices and outcomes.",
                f"With both leagues together, contracts in every price band paid out within "
                f"{n['pooled_gap_lowest']} to {n['pooled_gap_highest']} points of the midpoint price, and no "
                f"band's 95% range excludes zero. By league, the exception is NBA contracts under 5 cents: {n['nba_under5_actual']} paid out against "
                f"{n['nba_under5_expected']} expected, on only {n['nba_under5_payouts']} payouts. Longshot "
                f"bands are thin: the under-5-cent band has {n['credibility_under5']} of full credibility, or "
                f"{n['credibility_under5_by_game']} once contracts in the same game are allowed to move together. "
                f"So this is not proof the prices were right: for contracts under 5 cents, priced at "
                f"{n['under5_expected']} on average, the true rate could be {n['under5_plausible_low']} to "
                f"{n['under5_plausible_high']}.",
                "actual_vs_expected.png", 9 / 5),
        finding("2. What buyers lost was mostly the cost of trading.",
                f"Buying YES on every contract returned {n['return_all']} ({n['return_all_range']}). Of each "
                f"dollar spent, {n['spread_all']} went to the spread, {n['fee_all']} to the fee on a "
                f"{ORDER_SIZE}-contract order, and {n['midpoint_all']} ({n['midpoint_all_range']}) to the midpoint "
                f"being too high. The spread and fee, both known before buying, were {n['known_share_of_loss']} "
                f"of the loss. Longshots cost the most: under 5 cents the spread alone took {n['spread_under5']} "
                f"and buyers returned {n['return_under5']}, against {n['return_80_up']} at 80 cents and up. "
                f"Dropping any one game leaves the overall return in the range {n['return_all_without_one_game']}.",
                "cost_split.png", 11 / 5),
        finding("3. Kalshi matched a sportsbook-based curve, except on rare outcomes.",
                f"Overall accuracy was a tie: a Brier score of {n['brier_kalshi']} for Kalshi's midpoint and "
                f"{n['brier_book']} for a curve fitted to sportsbook lines (giving every contract the same chance "
                f"scores {n['brier_base_rate']}). On NFL lines far above the sportsbooks' middle, "
                f"{n['nfl_far_actual']} paid out; Kalshi said {n['nfl_far_kalshi']} and the curve "
                f"{n['nfl_far_book']}. The curve is this study's own extrapolation, so this is a finding about "
                f"the curve, not about sportsbooks.",
                "kalshi_vs_book_curve.png", 11 / 5),
    ], colWidths=[3.0 * inch, 4.4 * inch])
    findings.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 6), ("LEFTPADDING", (1, 0), (1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (0, -1), 10), ("RIGHTPADDING", (1, 0), (1, -1), 0),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))

    story = [
        Paragraph("Do Kalshi prices match what happened?", title),
        Spacer(1, 3),
        Paragraph(f"An actual-versus-expected study of {n['contracts']} settled player-prop contracts from "
                  f"{n['games']} NFL and NBA games, {n['period']}. {AUTHOR}.", byline),
        Spacer(1, 9),
        Paragraph(f"<b>Bottom line.</b> The data shows no measurable gap between Kalshi's prices and outcomes. "
                  f"Measured at the midpoint between the buy and sell price, contracts paid out within a few "
                  f"points of what the price implied, and what buyers lost was mostly the spread and the fee. "
                  f"With only {n['games']} games, a gap of up to {n['pooled_widest_bound']} points in a price band "
                  f"cannot be ruled out, so every result is reported with a range.", body),
        Spacer(1, 10),
        findings,
        Paragraph("<b>Method.</b> Prices were logged before each game by my own app from Kalshi's public API; "
                  "results are Kalshi's settlements. The price bands and return formula were set at the start "
                  "and not tuned to the results. Every 95% range comes from resampling whole games, because one "
                  "player's contracts in a game win or lose together. The sportsbook curve was set from earlier "
                  "seasons and not changed during the sample. "
                  "<b>Limits.</b> Five weeks of one season; games I chose to scan; the YES side only; contracts "
                  "settled part-way are left out. This is a study of prices, not betting advice. "
                  "The analysis code is covered by tests.", small),
    ]
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.55 * inch, rightMargin=0.55 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.45 * inch,
                            title="Do Kalshi prices match what happened?", author=AUTHOR, invariant=1)
    doc.build(story)
    if doc.page != 1:
        raise RuntimeError(f"The summary ran to {doc.page} pages; it must fit on one.")


def _style(ax):
    ax.set_facecolor(SURFACE)
    ax.tick_params(colors=MUTED, length=0)
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    for side in ax.spines.values():
        side.set_visible(False)


def chart_actual_vs_expected(tables, path):
    """Chart 1: actual minus expected by price band, NFL and NBA side by side, with 95% ranges."""
    fig, ax = plt.subplots(figsize=(9, 5), facecolor=SURFACE)
    _style(ax)
    ax.axhline(0, color=MUTED, linewidth=1)

    for league, shift in [("NFL", -0.14), ("NBA", 0.14)]:
        t = tables[(tables["sample"] == league) & (tables["scans"] == "all")]
        x = np.array([BAND_LABELS.index(b) for b in t["band"]]) + shift
        gap = t["gap"].to_numpy() * 100
        low = t["gap_low"].to_numpy() * 100
        high = t["gap_high"].to_numpy() * 100
        ax.errorbar(x, gap, yerr=[gap - low, high - gap], fmt="o", color=LEAGUE_COLORS[league], markersize=8,
                    markeredgecolor=SURFACE, markeredgewidth=2, elinewidth=2, capsize=0, label=league)

    ax.set_xticks(range(len(BAND_LABELS)))
    ax.set_xticklabels(BAND_LABELS, color=MUTED)
    ax.set_xlabel("Price band (midpoint between the buy and sell price)", color=MUTED)
    ax.set_ylabel("Actual minus expected (percentage points)", color=MUTED)

    fig.suptitle("Did Kalshi contracts pay out as often as their price implied?",
                 x=0.07, y=0.97, ha="left", fontsize=14, fontweight="bold", color=INK)
    ax.set_title("Above zero: paid out more often than the midpoint implied. "
                 "Lines are 95% ranges from resampling whole games.",
                 loc="left", fontsize=10, color=MUTED, pad=12)
    ax.legend(loc="lower left", frameon=False, ncol=2, labelcolor=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def chart_cost_split(tables, path):
    """Chart 2: for each league, the known cost of buying YES (spread + fee, stacked bars)
    against what buyers actually lost (dots with 95% ranges), as a share of the money spent."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 5), sharey=True, facecolor=SURFACE)
    x = np.arange(len(BAND_LABELS))

    for ax, league in zip(axes, ["NFL", "NBA"]):
        t = tables[(tables["sample"] == league) & (tables["scans"] == "all")].set_index("band").reindex(BAND_LABELS)
        spread = t["lost_to_spread"].to_numpy() * 100
        fee = t["lost_to_fee"].to_numpy() * 100
        lost = -t["return"].to_numpy() * 100
        lost_low = -t["return_high"].to_numpy() * 100
        lost_high = -t["return_low"].to_numpy() * 100

        _style(ax)
        ax.axhline(0, color=MUTED, linewidth=1)
        # the surface-colored edge leaves a thin gap between the two stacked parts
        ax.bar(x, spread, width=0.6, color=SPREAD_COLOR, edgecolor=SURFACE, linewidth=1.5, label="Spread")
        ax.bar(x, fee, width=0.6, bottom=spread, color=FEE_COLOR, edgecolor=SURFACE, linewidth=1.5, label="Fee")
        ax.errorbar(x, lost, yerr=[lost - lost_low, lost_high - lost], fmt="o", color=INK, markersize=8,
                    markeredgecolor=SURFACE, markeredgewidth=2, elinewidth=2, capsize=0,
                    label="What buyers lost (95% range)")
        ax.set_xticks(x)
        ax.set_xticklabels(BAND_LABELS, color=MUTED, fontsize=9)
        ax.set_title(league, loc="left", fontsize=11, fontweight="bold", color=INK)

    axes[0].set_ylabel("Share of the money spent (%)", color=MUTED)
    axes[0].legend(loc="upper right", frameon=False, labelcolor=INK)
    fig.supxlabel("Price band (midpoint between the buy and sell price)", color=MUTED, fontsize=10)
    fig.suptitle("Where a YES buyer's money went", x=0.06, y=0.97, ha="left",
                 fontsize=14, fontweight="bold", color=INK)
    fig.text(0.06, 0.84, f"Bars are the cost known when buying: the spread plus the fee on a {ORDER_SIZE}-contract order.\n"
             "A dot above its bar means the midpoint was too high as well.", fontsize=10, color=MUTED, linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def chart_kalshi_vs_book(tables, path):
    """Chart 3: for each league, actual minus expected for Kalshi's midpoint and for the book
    curve on the same contracts, by how far the line is from the books' middle."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 5), sharey=True, facecolor=SURFACE)

    for ax, league in zip(axes, ["NFL", "NBA"]):
        t = tables[(tables["sample"] == league) & (tables["scans"] == "all")]
        x = np.array([DISTANCE_LABELS.index(b) for b in t["distance_band"]])
        _style(ax)
        ax.axhline(0, color=MUTED, linewidth=1)
        for (source, name), shift in zip([(KALSHI, "kalshi"), (BOOK, "book")], [-0.14, 0.14]):
            color, marker = SOURCE_STYLE[source]
            gap = t[f"{name}_gap"].to_numpy() * 100
            low = t[f"{name}_gap_low"].to_numpy() * 100
            high = t[f"{name}_gap_high"].to_numpy() * 100
            ax.errorbar(x + shift, gap, yerr=[gap - low, high - gap], fmt=marker, color=color, markersize=8,
                        markeredgecolor=SURFACE, markeredgewidth=2, elinewidth=2, capsize=0, label=source)
        ax.set_xticks(range(len(DISTANCE_LABELS)))
        ax.set_xticklabels(DISTANCE_LABELS, color=MUTED, fontsize=9)
        ax.set_title(league, loc="left", fontsize=11, fontweight="bold", color=INK)

    axes[0].set_ylabel("Actual minus expected (percentage points)", color=MUTED)
    axes[1].legend(loc="upper right", frameon=False, labelcolor=INK)
    fig.supxlabel("Contract's line against the middle of the sportsbooks' lines, in typical swings of the stat",
                  color=MUTED, fontsize=10)
    fig.suptitle("Kalshi's midpoint against a curve fitted to sportsbook lines", x=0.06, y=0.97, ha="left",
                 fontsize=14, fontweight="bold", color=INK)
    fig.text(0.06, 0.84, "Same contracts for both. Above zero: paid out more often than that source expected.\n"
             "Lines are 95% ranges from resampling whole games.", fontsize=10, color=MUTED, linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("nfl_csv")
    parser.add_argument("nba_csv")
    parser.add_argument("--out", default=str(Path(__file__).parent), help="folder for output/ and charts/")
    args = parser.parse_args(argv)

    raw = load({"NFL": args.nfl_csv, "NBA": args.nba_csv})
    df = prepare(raw)
    with_mid = df[df["midpoint"].notna()]
    print(f"Rows read: {len(raw):,}. Settled part-way and left out: {len(raw) - len(df):,}. "
          f"Settled yes or no: {len(df):,}, of which {len(with_mid):,} have a midpoint.")
    print(f"Games: {df['game_id'].nunique()}. Player-games: "
          f"{df.groupby(['game_id', 'player']).ngroups:,}. Fresh scans: {df['fresh'].mean():.0%} of contracts.")

    bands = all_tables(df, band_table)
    costs = all_tables(df, cost_table)
    accuracy = all_tables(df, accuracy_table, early=True)
    rare = all_tables(df, rare_table)
    distance = all_tables(df, distance_table)
    sensitivity = sensitivity_table(df)
    order_sizes = order_size_table(df)

    print("\n\nOVERALL ACCURACY (Brier score, lower is better)\n")
    print(show_accuracy(accuracy))
    for title, tables, render in [
            ("ACTUAL VERSUS EXPECTED", bands, show),
            (f"WHERE A YES BUYER'S MONEY WENT (fee on a {ORDER_SIZE}-contract order)", costs, show_cost),
            (f"RARE OUTCOMES (given under a {RARE_BELOW:.0%} chance)", rare, show_rare),
            ("KALSHI VERSUS THE BOOK CURVE, BY DISTANCE FROM THE BOOKS' MIDDLE", distance, show_distance)]:
        print(f"\n\n{title}")
        for (scans, sample), table in tables.groupby(["scans", "sample"], sort=False):
            print(f"\n{sample}, {scans} scans")
            print(render(table))
    print("\n\nONE GAME DROPPED AT A TIME (% and points are shown as fractions)\n")
    print(sensitivity.drop(columns="key").round(4).to_string(index=False))
    print("\n\nRETURN FROM BUYING YES, BY ORDER SIZE (both leagues, as fractions)\n")
    print(order_sizes.round(3).to_string(index=False))

    out = Path(args.out)
    (out / "output").mkdir(parents=True, exist_ok=True)
    (out / "charts").mkdir(parents=True, exist_ok=True)
    for name, table in [("band_table", bands), ("cost_table", costs), ("accuracy_table", accuracy),
                        ("rare_table", rare), ("distance_table", distance),
                        ("sensitivity_table", sensitivity), ("order_size_table", order_sizes)]:
        table.to_csv(out / "output" / f"{name}.csv", index=False)
    chart_actual_vs_expected(bands, out / "charts" / "actual_vs_expected.png")
    chart_cost_split(costs, out / "charts" / "cost_split.png")
    chart_kalshi_vs_book(distance, out / "charts" / "kalshi_vs_book_curve.png")

    numbers = key_numbers(raw, df, bands, costs, accuracy, rare, distance, sensitivity, order_sizes)
    (out / "output" / "key_numbers.json").write_text(json.dumps(numbers, indent=2) + "\n")
    (out / "output" / "tables.md").write_text(
        markdown_tables(bands, costs, accuracy, rare, distance, sensitivity, order_sizes))
    write_summary_pdf(numbers, out / "charts", out / "one_page_summary.pdf")
    print(f"\nSaved tables to {out / 'output'}, charts to {out / 'charts'} and {out / 'one_page_summary.pdf'}")


if __name__ == "__main__":
    main()
