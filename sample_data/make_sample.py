"""Make small files of MADE-UP data so study.py can be run without the real record: two Kalshi
files and two prop files.

Run:  .venv/bin/python sample_data/make_sample.py

Nothing here comes from Kalshi or a sportsbook: the players, games, prices and results are
random numbers with the same columns and rough shape as the real data. Results from running the
study on these files mean nothing; they only show that the script works end to end.
"""
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
GAMES, PLAYERS = 12, 8
# Each player gets a ladder of lines, this many typical swings from the middle of the books' lines.
LADDER = [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]


def make_league(league, stat, middle_range, first_game, seed):
    rng = np.random.default_rng(seed)
    rows = []
    for g in range(GAMES):
        kickoff = pd.Timestamp(first_game, tz="UTC") + pd.Timedelta(days=2 * g)
        hours_before = 30 if g % 4 == 3 else 1.5      # every fourth game is scanned a day early
        for p in range(PLAYERS):
            middle = rng.uniform(*middle_range)
            swing = 5 + 0.3 * middle                   # typical game-to-game spread of the stat
            # What "really" happens swings a little more than the book curve allows for.
            outcome = rng.normal(middle, swing * 1.15)
            for steps in LADDER:
                line = round(middle + steps * swing) + 0.5
                books_over = 1 - NormalDist(middle, swing).cdf(line)
                market = 1 - NormalDist(middle, swing * 1.15).cdf(line) + rng.normal(0, 0.01)
                half_spread = rng.choice([0.01, 0.02])
                yes_ask = float(np.clip(round(market + half_spread, 2), 0.02, 0.99))
                yes_bid = float(np.clip(round(market - half_spread, 2), 0.01, yes_ask - 0.01))
                n = len(rows)
                rows.append({
                    "logged_at": (kickoff - pd.Timedelta(hours=hours_before)).isoformat(),
                    "game_time": kickoff.isoformat(),
                    "game": f"Sample Away {g + 1} @ Sample Home {g + 1}",
                    "player": f"Sample Player {g * PLAYERS + p + 1}",
                    "stat": stat,
                    "line": line,
                    "ticker": f"SAMPLE-{league}-{g:02d}-{p:02d}-{int(line)}",
                    "yes_ask": yes_ask,
                    # a few contracts have nobody selling NO, so no price to sell YES at
                    "no_ask": None if n % 53 == 52 else round(1 - yes_bid, 2),
                    "books_over": round(books_over, 4),
                    "books_under": round(1 - books_over, 4),
                    "middle": round(middle, 2),
                    "distance": round(abs(line - middle) / swing, 2),
                    # a few contracts settle part-way
                    "result": "scalar" if n % 97 == 96 else ("yes" if outcome > line else "no"),
                    "event_id": f"sample-{league.lower()}-{g:02d}",
                })
    return pd.DataFrame(rows)


def make_props(league, stat, middle_range, first_game, seed):
    """One made-up player prop per player and game, in the layout of the app's prop files."""
    rng = np.random.default_rng(seed)
    rows = []
    for g in range(GAMES):
        kickoff = pd.Timestamp(first_game, tz="UTC") + pd.Timedelta(days=2 * g)
        for p in range(PLAYERS):
            middle = rng.uniform(*middle_range)
            swing = 5 + 0.3 * middle
            line = round(middle) + 0.5
            market_over = float(np.clip(1 - NormalDist(middle, swing).cdf(line) + rng.normal(0, 0.02), 0.3, 0.7))
            actual = max(0.0, round(rng.normal(middle - 0.05 * swing, swing * 1.1)))
            pp_line = float(round(middle) - (1 if p % 3 == 0 else 0))   # whole number, sometimes below
            rows.append({
                "logged_at": (kickoff - pd.Timedelta(hours=2)).isoformat(),
                "model_version": "sample",
                "game_time": kickoff.isoformat(),
                "event_id": f"sample-{league.lower()}-{g:02d}",
                "player": f"Sample Player {g * PLAYERS + p + 1}",
                "stat": stat,
                "line": line,
                "market_over": round(market_over, 4),
                "model_over": round(float(np.clip(market_over + rng.normal(0, 0.05), 0.05, 0.95)), 4),
                "underdog_line": line,
                "underdog_market_over": round(market_over, 4),
                "underdog_model_over": round(float(np.clip(market_over + rng.normal(0, 0.05), 0.05, 0.95)), 4),
                "prizepicks_line": pp_line,
                "prizepicks_market_over": round(1 - NormalDist(middle, swing).cdf(pp_line), 4),
                "prizepicks_model_over": "",
                "actual": actual,
                "result": "over" if actual > line else "under",
            })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    make_league("NFL", "receivingYards", (25, 90), "2026-01-04T18:00:00", seed=1).to_csv(
        HERE / "nfl_sample.csv", index=False)
    make_league("NBA", "points", (8, 28), "2026-01-05T00:00:00", seed=2).to_csv(
        HERE / "nba_sample.csv", index=False)
    make_props("NFL", "receivingYards", (25, 90), "2026-01-04T18:00:00", seed=3).to_csv(
        HERE / "nfl_props_sample.csv", index=False)
    make_props("NBA", "points", (8, 28), "2026-01-05T00:00:00", seed=4).to_csv(
        HERE / "nba_props_sample.csv", index=False)
    print(f"Wrote the sample files to {HERE}")
