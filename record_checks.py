"""The rest of the app's pre-game record: sportsbook prices on player props, pick'em lines, the
old projection model, rules for buying Kalshi contracts, and how the book curve handles far tails.

study.py calls these functions; nothing here runs on its own. The inputs are the app's two prop
files (one row per player prop, with the sportsbooks' de-vigged chance of the over at the books'
line, the Underdog and PrizePicks lines, and what happened) and the same Kalshi files study.py
reads.

Every range is a 95% range from resampling whole games, as in study.py. Many of the groups below
were picked after looking at results (the rules for buying Kalshi contracts most of all), so a
range that excludes zero is weaker evidence than it looks. Only the paper trials are out of
sample: their rules were frozen on a start date and only later prices count.
"""
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

import study

PICK_MIN = 0.54        # a pick'em line "rated 54%+" is one the books give at least this chance
EDGE_MIN = 0.03        # a Kalshi side is bought when the books' chance beats its cost by more than 3%
NEAR_LINE = 1.0        # ... and its line is within one typical swing of the books' middle
LONGSHOT_MAX = 0.15    # a "cheap longshot" is a YES contract asking 15 cents or less
MODEL_RULE_SINCE = "2026-10-05"   # the projection-model tie-breaker was scored on fresh picks from here
YARDS = {"passingYards", "rushingYards", "receivingYards", "rushRecYards"}

STAT_NAMES = {
    "passingYards": "Passing yards", "passingTouchdowns": "Passing touchdowns", "rushingYards": "Rushing yards",
    "receivingYards": "Receiving yards", "receptions": "Receptions", "rushRecYards": "Rushing + receiving yards",
    "points": "Points", "rebounds": "Rebounds", "assists": "Assists", "threes": "Threes",
}

# The paper trials, copied from the app's strategies.py exactly as frozen on their start dates.
# A trial buys up to TRIAL_CONTRACTS contracts (fewer when fewer were on offer) on prices saved
# at most TRIAL_MAX_HOURS before the start, pays Kalshi's fee as charged on that order, and is
# scored at Kalshi's own settlement.
TRIAL_CONTRACTS = 10
TRIAL_MAX_HOURS = 6
TRIALS = [
    {"key": "kalshi-edge-3", "league": "NFL", "since": "2026-11-10", "stats": None, "sides": None,
     "max_distance": 1.0, "maker": False,
     "rule": "NFL, either side, edge over 3% after the taker fee, within 1 swing of the books' middle"},
    {"key": "kalshi-rush-unders", "league": "NFL", "since": "2026-11-10", "stats": {"rushingYards"},
     "sides": {"under"}, "max_distance": 1.0, "maker": False,
     "rule": "The same, rushing-yards unders only"},
    {"key": "kalshi-nfl-unders-maker", "league": "NFL", "since": "2026-11-23", "stats": None,
     "sides": {"under"}, "max_distance": 1.0, "maker": True,
     "rule": "NFL unders, edge over 3% at the maker fee, within 1 swing"},
    {"key": "kalshi-nfl-skewed-2sd", "league": "NFL", "since": "2026-11-23", "stats": None, "sides": None,
     "max_distance": 2.0, "maker": False,
     "rule": "NFL, either side, edge over 3% after the taker fee, within 2 swings (Log-Normal yards curve)"},
]

PROPS_COLUMNS = ["event_id", "player", "game_time", "logged_at", "stat", "line", "market_over", "model_over",
                 "result", "actual", "underdog_line", "underdog_market_over", "underdog_model_over",
                 "prizepicks_line", "prizepicks_market_over", "prizepicks_model_over"]


# ---------- shared pieces ----------

def _ratio_with_range(part, top, bottom, resamples, seed):
    """sum(top) / sum(bottom), and its 95% range from resampling the games in `part`."""
    per_game = part.groupby("game_id")[[top, bottom]].sum()
    draws = study._game_draws(len(per_game), resamples, seed)
    with np.errstate(invalid="ignore", divide="ignore"):
        resampled = (draws @ per_game[top].to_numpy()) / (draws @ per_game[bottom].to_numpy())
    low, high = np.nanpercentile(resampled, [2.5, 97.5])
    return per_game[top].sum() / per_game[bottom].sum(), low, high


def _hours_before(table):
    kickoff = pd.to_datetime(table["game_time"], utc=True)
    scanned = pd.to_datetime(table["logged_at"], utc=True)
    return (kickoff - scanned).dt.total_seconds() / 3600


# ---------- sportsbook props ----------

def load_props(paths):
    """Read one prop file per league ({"NFL": path, "NBA": path})."""
    tables = []
    for league, path in paths.items():
        table = pd.read_csv(path)
        table["league"] = league
        if len(table):
            tables.append(table)
    props = pd.concat(tables, ignore_index=True)
    missing = [c for c in PROPS_COLUMNS if c not in props.columns]
    if missing:
        raise ValueError(f"Prop files are missing columns: {missing}")
    problems = []
    if not props["market_over"].dropna().between(0, 1).all():
        problems.append("a sportsbook chance is outside 0 to 1")
    unknown = set(props["result"].dropna().unique()) - {"over", "under", "push", "did not play"}
    if unknown:
        problems.append(f"unknown result values {sorted(unknown)}")
    if _hours_before(props).isna().any():
        problems.append("a scan time or game time is blank")
    if problems:
        raise ValueError("Prop files failed checks: " + "; ".join(problems))
    props["game_id"] = props["league"] + ":" + props["event_id"].astype(str)
    props["hours_before"] = _hours_before(props)
    # a prop logged after the start could already reflect what happened
    return props[props["hours_before"] >= 0].copy()


def over_bias_table(props, resamples=study.RESAMPLES, seed=study.SEED):
    """How often the over hit, against the sportsbooks' de-vigged chance of the over at their
    own line, for each league and each stat. Pushes and players who did not play are left out."""
    graded = props[props["result"].isin(["over", "under"])].assign(one=1.0)
    graded["went_over"] = (graded["result"] == "over").astype(float)
    graded["gap"] = graded["went_over"] - graded["market_over"]
    rows = []
    for league, part in graded.groupby("league", sort=False):
        groups = [("All stats", part)] + [(STAT_NAMES.get(s, s), sub) for s, sub in part.groupby("stat")]
        for label, sub in groups:
            gap, low, high = _ratio_with_range(sub, "gap", "one", resamples, seed)
            rows.append({"league": league, "stat": label, "props": len(sub), "games": sub["game_id"].nunique(),
                         "actual_over": sub["went_over"].mean(), "books_over": sub["market_over"].mean(),
                         "gap": gap, "gap_low": low, "gap_high": high})
    return pd.DataFrame(rows)


def _pickem_picks(props):
    """One row per pick'em line (Underdog or PrizePicks) with a sportsbook chance at that line:
    the side the books favour, its chance, and whether it hit. Pushes are left out."""
    known = props[props["actual"].notna()]
    picks = []
    for app, prefix in [("Underdog", "underdog"), ("PrizePicks", "prizepicks")]:
        p = known[known[f"{prefix}_line"].notna() & known[f"{prefix}_market_over"].notna()].copy()
        p["app"] = app
        p["app_line"] = p[f"{prefix}_line"]
        p["over_chance"] = p[f"{prefix}_market_over"]
        p["app_model_over"] = p[f"{prefix}_model_over"]
        picks.append(p[p["actual"] != p["app_line"]])
    picks = pd.concat(picks, ignore_index=True)
    picks["side_over"] = picks["over_chance"] >= 0.5
    picks["chance"] = np.where(picks["side_over"], picks["over_chance"], 1 - picks["over_chance"])
    picks["hit"] = ((picks["actual"] > picks["app_line"]) == picks["side_over"]).astype(float)
    # the old projection model "agrees" when it gives the books' side at least the books' chance
    agrees = np.where(picks["side_over"], picks["app_model_over"] >= picks["over_chance"],
                      picks["app_model_over"] <= picks["over_chance"]).astype(float)
    picks["model_agrees"] = np.where(picks["app_model_over"].isna(), np.nan, agrees)
    return picks


def pickem_table(props, resamples=study.RESAMPLES, seed=study.SEED):
    """Did pick'em lines hit as often as the sportsbooks said? Every pick takes the side the books
    favour at the app's line. Also the over on PrizePicks whole-number receptions lines set below
    the books' line where the books favour the over, and the old model's tie-breaker on fresh scans."""
    picks = _pickem_picks(props)
    picks["one"] = 1.0
    picks["miss"] = picks["hit"] - picks["chance"]
    rows = []

    def add(league, group, part, hit_column="hit", chance_column="chance"):
        if part.empty:
            return
        part = part.assign(_gap=part[hit_column] - part[chance_column])
        gap, low, high = _ratio_with_range(part, "_gap", "one", resamples, seed)
        rows.append({"league": league, "group": group, "picks": len(part), "games": part["game_id"].nunique(),
                     "hit": part[hit_column].mean(), "expected": part[chance_column].mean(),
                     "gap": gap, "gap_low": low, "gap_high": high})

    for league, part in picks.groupby("league", sort=False):
        add(league, "Every pick", part)
        add(league, f"Books rated {PICK_MIN:.0%}+", part[part["chance"] >= PICK_MIN])
        if league == "NFL":
            below = part[(part["app"] == "PrizePicks") & (part["stat"] == "receptions")
                         & (part["app_line"] % 1 == 0) & (part["app_line"] < part["line"]) & part["side_over"]]
            below = below.assign(over_hit=(below["actual"] > below["app_line"]).astype(float))
            add(league, "PrizePicks whole-number receptions below the books' line, over favoured", below,
                "over_hit", "over_chance")
            fresh = part[(part["logged_at"] >= MODEL_RULE_SINCE) & (part["hours_before"] < study.FRESH_HOURS)
                         & part["model_agrees"].notna()]
            add(league, "Fresh scans, model agrees", fresh[fresh["model_agrees"] == 1])
            add(league, "Fresh scans, model disagrees", fresh[fresh["model_agrees"] == 0])
    return pd.DataFrame(rows)


def model_accuracy_table(props, resamples=study.RESAMPLES, seed=study.SEED):
    """Brier score of the old projection model against the sportsbooks, on the same NFL props."""
    graded = props[props["result"].isin(["over", "under"]) & props["model_over"].notna()].assign(one=1.0)
    rows = []
    for league, part in graded.groupby("league", sort=False):
        went_over = (part["result"] == "over").astype(float)
        part = part.assign(model_sq=(part["model_over"] - went_over) ** 2,
                           books_sq=(part["market_over"] - went_over) ** 2)
        part["diff"] = part["model_sq"] - part["books_sq"]
        diff, low, high = _ratio_with_range(part, "diff", "one", resamples, seed)
        rows.append({"league": league, "props": len(part), "games": part["game_id"].nunique(),
                     "brier_model": part["model_sq"].mean(), "brier_books": part["books_sq"].mean(),
                     "model_minus_books": diff, "difference_low": low, "difference_high": high})
    return pd.DataFrame(rows)


# ---------- Kalshi rules ----------

def _kalshi_sides(raw):
    """Both sides of every settled Kalshi contract priced before the start: one row per side,
    with its ask, the books' chance for that side, and what it paid. A contract settled part-way
    pays its settlement value (YES) or the rest (NO)."""
    df = raw.copy()
    df["hours_before"] = _hours_before(df)
    df = df[df["result"].isin(["yes", "no", "scalar"]) & (df["hours_before"] >= 0)].copy()
    settlement = pd.to_numeric(df["settlement"], errors="coerce") if "settlement" in df else np.nan
    yes_paid = np.where(df["result"] == "yes", 1.0, np.where(df["result"] == "no", 0.0, settlement))
    df["game_id"] = df["league"] + ":" + df["event_id"].astype(str)
    sides = []
    for side, price, chance, size, paid in [("over", "yes_ask", "books_over", "yes_ask_size", yes_paid),
                                            ("under", "no_ask", "books_under", "no_ask_size", 1 - yes_paid)]:
        s = df[["league", "game_id", "stat", "distance", "logged_at", "hours_before"]].copy()
        s["side"] = side
        s["price"] = df[price]
        s["chance"] = df[chance]
        s["size"] = pd.to_numeric(df[size], errors="coerce") if size in df else np.nan
        s["paid"] = paid
        sides.append(s)
    sides = pd.concat(sides, ignore_index=True)
    return sides[sides["price"].notna() & sides["chance"].notna() & sides["paid"].notna()]


# (league, rule, sides, stats, minimum edge or None for the longshot rule)
BACKTEST_RULES = [
    ("NFL", "Any edge after the fee, within 1 swing, both sides", None, None, 0.0),
    ("NFL", "Edge over 3%, within 1 swing, both sides", None, None, EDGE_MIN),
    ("NFL", "Edge over 3%, within 1 swing, overs", {"over"}, None, EDGE_MIN),
    ("NFL", "Edge over 3%, within 1 swing, unders", {"under"}, None, EDGE_MIN),
    ("NFL", "Edge over 3%, within 1 swing, rushing-yards unders", {"under"}, {"rushingYards"}, EDGE_MIN),
    ("NBA", "Any edge after the fee, within 1 swing, both sides", None, None, 0.0),
    ("NBA", "Edge over 3%, within 1 swing, both sides", None, None, EDGE_MIN),
    ("NBA", "Edge over 3%, within 1 swing, overs", {"over"}, None, EDGE_MIN),
    ("NBA", "Edge over 3%, within 1 swing, unders", {"under"}, None, EDGE_MIN),
    ("NFL", "Every YES at 15c or less", {"over"}, None, None),
    ("NBA", "Every YES at 15c or less", {"over"}, None, None),
]


def backtest_table(raw, resamples=study.RESAMPLES, seed=study.SEED):
    """Rules for buying Kalshi contracts, tried on the whole record after the fact. One contract
    per side, at the ask, with the taker fee before rounding (as on a large order), the way the
    app's own back-test counts it. These rules were found by looking at this same data, so the
    returns are in-sample."""
    sides = _kalshi_sides(raw)
    sides["fee"] = study.FEE_RATE * sides["price"] * (1 - sides["price"])
    sides["cost"] = sides["price"] + sides["fee"]
    sides["edge"] = sides["chance"] / sides["cost"] - 1
    sides["profit"] = sides["paid"] - sides["cost"]
    rows = []
    for league, rule, side_set, stat_set, min_edge in BACKTEST_RULES:
        part = sides[sides["league"] == league]
        if side_set:
            part = part[part["side"].isin(side_set)]
        if stat_set:
            part = part[part["stat"].isin(stat_set)]
        if min_edge is not None:
            part = part[(part["edge"] > min_edge) & (part["distance"] <= NEAR_LINE)]
        else:
            part = part[part["price"] <= LONGSHOT_MAX]
        if part.empty:
            continue
        ret, low, high = _ratio_with_range(part, "profit", "cost", resamples, seed)
        rows.append({"league": league, "rule": rule, "buys": len(part), "games": part["game_id"].nunique(),
                     "average_price": part["price"].mean(), "books_chance": part["chance"].mean(),
                     "paid_out": part["paid"].mean(), "return": ret, "return_low": low, "return_high": high})
    return pd.DataFrame(rows)


def paper_trial_table(raw, resamples=study.RESAMPLES, seed=study.SEED):
    """The paper trials, scored the same way the app scores them. Out of sample: only prices
    saved on or after each trial's start date count. A range is shown from 10 games up."""
    sides = _kalshi_sides(raw)
    rows = []
    for trial in TRIALS:
        part = sides[(sides["league"] == trial["league"]) & (sides["logged_at"].str[:10] >= trial["since"])
                     & sides["size"].notna() & sides["hours_before"].between(0, TRIAL_MAX_HOURS)
                     & (sides["distance"] <= trial["max_distance"])].copy()
        if trial["stats"]:
            part = part[part["stat"].isin(trial["stats"])]
        if trial["sides"]:
            part = part[part["side"].isin(trial["sides"])]
        part["contracts"] = np.minimum(TRIAL_CONTRACTS, part["size"].fillna(0).astype(int))
        part = part[part["contracts"] >= 1]
        rate = study.MAKER_FEE_RATE if trial["maker"] else study.FEE_RATE
        order_fee = [study.fee_per_contract(p, c, rate=rate) * c for p, c in zip(part["price"], part["contracts"])]
        part["cost"] = part["contracts"] * part["price"] + np.array(order_fee, dtype=float)
        part = part[part["chance"] * part["contracts"] / part["cost"] - 1 > EDGE_MIN]
        part["profit"] = part["contracts"] * part["paid"] - part["cost"]
        row = {"trial": trial["key"], "rule": trial["rule"], "since": trial["since"], "buys": len(part),
               "games": part["game_id"].nunique(), "contracts": int(part["contracts"].sum()),
               "return": np.nan, "return_low": np.nan, "return_high": np.nan}
        if len(part):
            ret, low, high = _ratio_with_range(part, "profit", "cost", resamples, seed)
            row["return"] = ret
            if row["games"] >= 10:
                row["return_low"], row["return_high"] = low, high
        rows.append(row)
    return pd.DataFrame(rows)


# ---------- far tails ----------

def _lognormal_over(line, middle, swing):
    """The chance of going over `line` on a Log-Normal curve with median `middle` and standard
    deviation `swing`: the curve the app now uses for yards, because big games make the right
    tail fatter than a Normal curve allows."""
    m = max(middle, 1.0)
    sigma = math.sqrt(math.log(1 + (swing / m) ** 2))
    return 1 - NormalDist(math.log(m), sigma).cdf(math.log(line)) if line > 0 else 1.0


def tail_table(df, resamples=study.RESAMPLES, seed=study.SEED):
    """NFL yards contracts with a line 2 or more swings above the books' middle: how often they
    paid out, against the Normal book curve the data was priced with, a Log-Normal curve worked
    out from the same middle and swing, and Kalshi's midpoint. `df` is study.prepare()'s table."""
    curve = study.on_book_curve(df)
    far = curve[(curve["league"] == "NFL") & curve["stat"].isin(YARDS) & (curve["line"] > curve["middle"])
                & (curve["distance"] >= 2) & curve["midpoint"].notna()].copy()
    if far.empty:
        return pd.DataFrame()
    swing = (far["line"] - far["middle"]) / far["distance"]
    far["lognormal"] = [_lognormal_over(l, m, s) for l, m, s in zip(far["line"], far["middle"], swing)]
    far["one"] = 1.0
    rows = []
    for name, column in [("Normal book curve", "books_over"), ("Log-Normal curve", "lognormal"),
                         (study.KALSHI, "midpoint")]:
        far["_gap"] = far["happened"] - far[column]
        gap, low, high = _ratio_with_range(far, "_gap", "one", resamples, seed)
        rows.append({"source": name, "contracts": len(far), "games": far["game_id"].nunique(),
                     "actual": far["happened"].mean(), "expected": far[column].mean(),
                     "gap": gap, "gap_low": low, "gap_high": high})
    return pd.DataFrame(rows)


# ---------- for the README ----------

def _pct(x, signed=False):
    return study._pct(x, signed=signed)


def _gap_text(r, name="gap"):
    return f"{study._signed(r[name] * 100)} ({study._span(r[name + '_low'], r[name + '_high'])})"


def _return_text(r):
    if pd.isna(r["return"]):
        return "none graded yet"
    if pd.isna(r["return_low"]):
        return f"{_pct(r['return'], signed=True)} (too few games for a range)"
    return f"{_pct(r['return'], signed=True)} ({_pct(r['return_low'], signed=True)} to {_pct(r['return_high'], signed=True)})"


def record_numbers(over_bias, pickem, model, backtest, trials, tails):
    """Key figures as text, checked against the README like study.key_numbers()."""
    n = {}

    def row(table, **match):
        sub = table
        for k, v in match.items():
            sub = sub[sub[k] == v]
        return sub.iloc[0] if len(sub) else None

    if over_bias is not None and len(over_bias):
        for league in ["NFL", "NBA"]:
            r = row(over_bias, league=league, stat="All stats")
            if r is not None:
                key = league.lower()
                n[f"{key}_props"] = f"{r['props']:,}"
                n[f"{key}_props_games"] = f"{r['games']}"
                n[f"{key}_over_gap"] = study._signed(r["gap"] * 100)
                n[f"{key}_over_gap_range"] = study._span(r["gap_low"], r["gap_high"])
        for league, stat, key in [("NFL", "Rushing yards", "nfl_rushing"), ("NFL", "Receiving yards", "nfl_receiving"),
                                  ("NBA", "Points", "nba_points")]:
            r = row(over_bias, league=league, stat=stat)
            if r is not None:
                n[f"{key}_over_gap"] = study._signed(r["gap"] * 100)
                n[f"{key}_over_gap_range"] = study._span(r["gap_low"], r["gap_high"])
                n[f"{key}_props"] = f"{r['props']:,}"
    if pickem is not None and len(pickem):
        for league in ["NFL", "NBA"]:
            r = row(pickem, league=league, group=f"Books rated {PICK_MIN:.0%}+")
            if r is not None:
                n[f"{league.lower()}_pick54_hit"] = _pct(r["hit"])
                n[f"{league.lower()}_pick54_picks"] = f"{r['picks']}"
        r = row(pickem, league="NFL", group="PrizePicks whole-number receptions below the books' line, over favoured")
        if r is not None:
            n["prizepicks_receptions_hit"] = _pct(r["hit"])
            n["prizepicks_receptions_expected"] = _pct(r["expected"])
            n["prizepicks_receptions_picks"] = f"{r['picks']}"
        agree = row(pickem, league="NFL", group="Fresh scans, model agrees")
        disagree = row(pickem, league="NFL", group="Fresh scans, model disagrees")
        if agree is not None and disagree is not None:
            n["model_agrees_hit"] = _pct(agree["hit"])
            n["model_disagrees_hit"] = _pct(disagree["hit"])
            n["model_rule_picks"] = f"{agree['picks'] + disagree['picks']:,}"
    if model is not None and len(model):
        r = row(model, league="NFL")
        if r is not None:
            n["brier_projection_model"] = f"{r['brier_model']:.3f}"
            n["brier_sportsbooks"] = f"{r['brier_books']:.3f}"
    if backtest is not None and len(backtest):
        for league, rule, key in [("NFL", "Edge over 3%, within 1 swing, unders", "nfl_unders"),
                                  ("NFL", "Edge over 3%, within 1 swing, both sides", "nfl_edge"),
                                  ("NBA", "Any edge after the fee, within 1 swing, both sides", "nba_any_edge"),
                                  ("NBA", "Edge over 3%, within 1 swing, both sides", "nba_edge"),
                                  ("NBA", "Edge over 3%, within 1 swing, overs", "nba_overs"),
                                  ("NFL", "Every YES at 15c or less", "nfl_longshots"),
                                  ("NBA", "Every YES at 15c or less", "nba_longshots")]:
            r = row(backtest, league=league, rule=rule)
            if r is not None:
                n[f"{key}_return"] = _pct(r["return"], signed=True)
                n[f"{key}_return_range"] = f"{_pct(r['return_low'], signed=True)} to {_pct(r['return_high'], signed=True)}"
                n[f"{key}_buys"] = f"{r['buys']:,}"
                n[f"{key}_games"] = f"{r['games']}"
    if trials is not None and len(trials):
        for key in ["kalshi-edge-3", "kalshi-rush-unders"]:
            r = row(trials, trial=key)
            if r is not None and r["buys"]:
                short = key.replace("kalshi-", "").replace("-", "_")
                n[f"trial_{short}_buys"] = f"{r['buys']}"
                n[f"trial_{short}_games"] = f"{r['games']}"
                n[f"trial_{short}_return"] = _pct(r["return"], signed=True)
    if tails is not None and len(tails):
        for source, key in [("Normal book curve", "normal"), ("Log-Normal curve", "lognormal"), (study.KALSHI, "kalshi")]:
            r = row(tails, source=source)
            if r is not None:
                n[f"tail_{key}"] = _pct(r["expected"])
        n["tail_actual"] = _pct(tails["actual"].iloc[0])
        n["tail_contracts"] = f"{tails['contracts'].iloc[0]:,}"
    return n


def record_markdown(over_bias, pickem, model, backtest, trials, tails):
    """The record tables for the README, as Markdown."""
    tables = {}
    if over_bias is not None and len(over_bias):
        tables["Sportsbook overs: actual against the books' chance"] = study._md(
            ["League", "Stat", "Props", "Games", "Over hit", "Books said", "Gap in points (95% range)"],
            [[r["league"], r["stat"], f"{r['props']:,}", r["games"], _pct(r["actual_over"]), _pct(r["books_over"]),
              _gap_text(r)] for _, r in over_bias.iterrows()])
    if pickem is not None and len(pickem):
        tables["Pick'em lines and the old model's tie-breaker"] = study._md(
            ["League", "Group", "Picks", "Games", "Hit", "Books said", "Gap in points (95% range)"],
            [[r["league"], r["group"], f"{r['picks']:,}", r["games"], _pct(r["hit"]), _pct(r["expected"]),
              _gap_text(r)] for _, r in pickem.iterrows()])
    if model is not None and len(model):
        tables["Old projection model against the sportsbooks"] = study._md(
            ["League", "Props", "Games", "Brier, model", "Brier, sportsbooks", "Model minus books (95% range)"],
            [[r["league"], f"{r['props']:,}", r["games"], f"{r['brier_model']:.3f}", f"{r['brier_books']:.3f}",
              f"{r['model_minus_books']:+.3f} ({r['difference_low']:+.3f} to {r['difference_high']:+.3f})"]
             for _, r in model.iterrows()])
    if backtest is not None and len(backtest):
        tables["Kalshi buying rules tried on the whole record (in-sample)"] = study._md(
            ["League", "Rule", "Buys", "Games", "Average price", "Books said", "Paid out", "Return (95% range)"],
            [[r["league"], r["rule"], f"{r['buys']:,}", r["games"], f"{r['average_price'] * 100:.1f}c",
              _pct(r["books_chance"]), _pct(r["paid_out"]),
              f"{_pct(r['return'], signed=True)} ({_pct(r['return_low'], signed=True)} to {_pct(r['return_high'], signed=True)})"]
             for _, r in backtest.iterrows()])
    if trials is not None and len(trials):
        tables["Paper trials (out of sample)"] = study._md(
            ["Trial", "Rule", "Since", "Buys", "Games", "Return (95% range)"],
            [[r["trial"], r["rule"], r["since"], r["buys"], r["games"], _return_text(r)] for _, r in trials.iterrows()])
    if tails is not None and len(tails):
        tables["NFL yards lines 2+ swings above the middle"] = study._md(
            ["Source", "Contracts", "Games", "Paid out", "Source said", "Gap in points (95% range)"],
            [[r["source"], f"{r['contracts']:,}", r["games"], _pct(r["actual"]), _pct(r["expected"]), _gap_text(r)]
             for _, r in tails.iterrows()])
    return "\n\n".join(f"## {title}\n\n{table}" for title, table in tables.items()) + "\n" if tables else ""
