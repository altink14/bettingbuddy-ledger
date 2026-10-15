"""
Hand-computed checks for audit/reconcile_live_record.py (standard library only).

    python audit/test_reconcile_live_record.py

Expected values are worked out here from the plan's formulas with literal
numbers, not by calling the checker's own helpers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reconcile_live_record as rlr  # noqa: E402

Z = 1.959963984540054

# Copied from the synthetic fixture's schema (Tests/live_eval_fixture.py).
PREDICTIONS_LOG_DDL = """
CREATE TABLE predictions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL, log_date TEXT NOT NULL, sport TEXT NOT NULL,
    sportsbook TEXT NOT NULL, game_key TEXT NOT NULL, home_team TEXT NOT NULL,
    away_team TEXT NOT NULL, game_start_time_utc TEXT NOT NULL,
    home_ml REAL, away_ml REAL, ou_line REAL, predicted_winner TEXT, winner_confidence REAL,
    ou_prediction TEXT, ou_confidence REAL, ev_home REAL, ev_away REAL, model TEXT,
    actual_winner TEXT, actual_total REAL, closing_home_ml REAL, closing_away_ml REAL,
    closing_captured_at TEXT, closing_minutes_before_tip REAL, clv REAL, closing_provenance TEXT,
    why_json TEXT, closing_confirmed_at TEXT, closing_confirmed_by TEXT, clv_prob REAL,
    clv_status TEXT, consensus_close_prob REAL, consensus_books INTEGER, consensus_provenance TEXT,
    clv_consensus_prob REAL, clv_consensus_price REAL, clv_consensus_status TEXT, clv_method TEXT,
    clv_settled_at TEXT, result_source TEXT, result_source_ref TEXT, result_confirmed_at TEXT,
    result_conflict TEXT,
    CHECK (logged_at < game_start_time_utc),
    UNIQUE (log_date, sportsbook, game_key)
)
"""
TEAM_DDL = [
    """CREATE TABLE team_metadata (team_id INTEGER PRIMARY KEY, full_name TEXT, abbreviation TEXT,
       nickname TEXT, city TEXT, state TEXT, year_founded INTEGER, conference TEXT, division TEXT,
       fetched_at TEXT)""",
    """CREATE TABLE game_results (game_id TEXT NOT NULL, team_id INTEGER NOT NULL, season TEXT,
       season_type TEXT, game_date TEXT, team_abbr TEXT, team_name TEXT, matchup TEXT, wl TEXT,
       pts INTEGER, PRIMARY KEY (game_id, team_id))""",
    """CREATE TABLE espn_box_scores (espn_event_id TEXT PRIMARY KEY, game_date TEXT,
       home_team_id INTEGER, away_team_id INTEGER, home_pts INTEGER, away_pts INTEGER)""",
]

BOS, LAC, MIA, NYK = 1610612738, 1610612746, 1610612748, 1610612752
TEAMS = [(BOS, "Boston Celtics", "BOS"), (LAC, "Los Angeles Clippers", "LAC"),
         (MIA, "Miami Heat", "MIA"), (NYK, "New York Knicks", "NYK")]
ABBR = {t: a for t, _, a in TEAMS}

# (game id, date, home, away, home won)
LEAGUE_GAMES = [
    ("0022600001", "2026-10-21", BOS, MIA, True),
    ("0022600002", "2026-10-22", LAC, NYK, False),
    ("0022600003", "2026-10-23", MIA, LAC, True),
    ("0022600010", "2026-10-25", NYK, BOS, False),   # G1
    ("0022600011", "2026-10-26", LAC, MIA, True),    # G2
    ("0022600012", "2026-10-27", BOS, NYK, True),    # G3
]

M = "candidate_2026-08"
LEDGER_ROWS = [
    # id 1: G1, graded from the league's box score
    dict(logged_at="2026-10-25T15:00:00Z", log_date="2026-10-25", sport="NBA", sportsbook="fanduel",
         game_key="New York Knicks:Boston Celtics", home_team="New York Knicks", away_team="Boston Celtics",
         game_start_time_utc="2026-10-25T23:30:00Z", home_ml=130.0, away_ml=-150.0,
         predicted_winner="Boston Celtics", winner_confidence=60.0, model=M, actual_winner="Boston Celtics",
         result_source="nba.com", result_source_ref="0022600010",
         clv_status="priced", closing_home_ml=140.0, closing_away_ml=-160.0, closing_provenance="observed",
         clv_prob=0.01, clv_consensus_status="priced", clv_consensus_prob=0.02, consensus_provenance="observed"),
    # id 2: the same game at another book (excluded)
    dict(logged_at="2026-10-25T15:00:00Z", log_date="2026-10-25", sport="NBA", sportsbook="draftkings",
         game_key="New York Knicks:Boston Celtics", home_team="New York Knicks", away_team="Boston Celtics",
         game_start_time_utc="2026-10-25T23:30:00Z", home_ml=130.0, away_ml=-150.0,
         predicted_winner="New York Knicks", winner_confidence=51.0, model=M, actual_winner="Boston Celtics",
         result_source="nba.com", result_source_ref="0022600010"),
    # id 3: G2, ESPN-graded; tip 01:00Z = 21:00 Eastern the day before, matched by teams + Eastern date
    dict(logged_at="2026-10-26T14:00:00Z", log_date="2026-10-26", sport="NBA", sportsbook="fanduel",
         game_key="LA Clippers:Miami Heat", home_team="LA Clippers", away_team="Miami Heat",
         game_start_time_utc="2026-10-27T01:00:00Z", home_ml=-120.0, away_ml=100.0,
         predicted_winner="Miami Heat", winner_confidence=55.0, model=M, actual_winner="LA Clippers",
         result_source="espn", result_source_ref="401000002", result_confirmed_at="2026-10-27T20:00:00Z",
         clv_status="close_too_early", closing_home_ml=-125.0, closing_away_ml=105.0,
         clv_consensus_status="too_few_books"),
    # id 4: G3, pick'em prices, reconstructed close
    dict(logged_at="2026-10-26T13:00:00Z", log_date="2026-10-26", sport="NBA", sportsbook="fanduel",
         game_key="Boston Celtics:New York Knicks", home_team="Boston Celtics", away_team="New York Knicks",
         game_start_time_utc="2026-10-28T00:00:00Z", home_ml=-110.0, away_ml=-110.0,
         predicted_winner="Boston Celtics", winner_confidence=70.0, model=M, actual_winner="Boston Celtics",
         result_source="nba.com", result_source_ref="0022600012",
         clv_status="priced", closing_home_ml=-105.0, closing_away_ml=-115.0, closing_provenance="reconstructed",
         clv_prob=-0.005, clv_consensus_status="too_few_books"),
    # id 5: G3 logged again a day later with another pick (the later log never counts)
    dict(logged_at="2026-10-27T13:00:00Z", log_date="2026-10-27", sport="NBA", sportsbook="fanduel",
         game_key="Boston Celtics:New York Knicks", home_team="Boston Celtics", away_team="New York Knicks",
         game_start_time_utc="2026-10-28T00:00:00Z", home_ml=-110.0, away_ml=-110.0,
         predicted_winner="New York Knicks", winner_confidence=51.0, model=M, actual_winner="Boston Celtics",
         result_source="nba.com", result_source_ref="0022600012", clv_status="priced",
         closing_home_ml=-105.0, closing_away_ml=-115.0, closing_provenance="reconstructed", clv_prob=-0.005),
]


def build_three_game_dbs(d: str, ledger_rows=None) -> tuple[str, str]:
    odds, team = os.path.join(d, "OddsData.sqlite"), os.path.join(d, "TeamData.sqlite")
    oc = sqlite3.connect(odds)
    oc.execute(PREDICTIONS_LOG_DDL)
    for r in (LEDGER_ROWS if ledger_rows is None else ledger_rows):
        cols = list(r)
        oc.execute(f"INSERT INTO predictions_log ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [r[c] for c in cols])
    oc.commit(); oc.close()
    tc = sqlite3.connect(team)
    for ddl in TEAM_DDL:
        tc.execute(ddl)
    tc.executemany("INSERT INTO team_metadata (team_id, full_name, abbreviation) VALUES (?,?,?)", TEAMS)
    for gid, day, h, a, hw in LEAGUE_GAMES:
        tc.execute("INSERT INTO game_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (gid, h, "2026-27", "Regular Season", day, ABBR[h], "", f"{ABBR[h]} vs. {ABBR[a]}",
                    "W" if hw else "L", 100))
        tc.execute("INSERT INTO game_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (gid, a, "2026-27", "Regular Season", day, ABBR[a], "", f"{ABBR[a]} @ {ABBR[h]}",
                    "L" if hw else "W", 90))
    tc.execute("INSERT INTO espn_box_scores VALUES ('401000002','2026-10-26',?,?,110,100)", (LAC, MIA))
    tc.commit(); tc.close()
    return odds, team


class Formulas(unittest.TestCase):
    def test_wilson_7_of_10(self):
        lo, hi = rlr.wilson(7, 10)
        # Wilson bounds are the roots p of (0.7 - p)^2 = z^2 p (1 - p) / 10.
        for p in (lo, hi):
            self.assertAlmostEqual((0.7 - p) ** 2, Z * Z * p * (1 - p) / 10, places=13)
        self.assertAlmostEqual(lo, 0.396780, places=5)
        self.assertAlmostEqual(hi, 0.892208, places=5)
        self.assertEqual(rlr.wilson(0, 0), [None, None])

    def test_shin_minus150_plus130(self):
        pi_h = 1 / (1 + 100 / 150)   # 0.6
        pi_a = 1 / (1 + 130 / 100)   # 1/2.3
        beta = pi_h + pi_a
        self.assertAlmostEqual(rlr.implied(-150), 0.6, places=15)
        self.assertAlmostEqual(rlr.implied(130), 1 / 2.3, places=15)
        ph, pa = rlr.shin_pair(pi_h, pi_a)
        self.assertAlmostEqual(ph + pa, 1.0, places=12)
        prop_h = pi_h / beta
        # The favourite loses less of the margin under Shin than under proportional.
        self.assertGreater(ph, prop_h)
        self.assertLess(pi_h - ph, pi_h - prop_h)
        # Two outcomes have a closed form: (1-z) p^2 + z p = pi^2 / beta for both sides
        # gives p_h = 1/2 + (pi_h^2 - pi_a^2) / (2 beta).
        self.assertAlmostEqual(ph, 0.5 + (pi_h ** 2 - pi_a ** 2) / (2 * beta), places=12)
        self.assertAlmostEqual(ph, 0.582609, places=6)
        z = rlr.shin_z(pi_h, pi_a)
        self.assertTrue(0 < z < 1)
        for p, pi in ((ph, pi_h), (pa, pi_a)):
            self.assertAlmostEqual((1 - z) * p * p + z * p, pi * pi / beta, places=12)
        # No margin: both methods are the plain normalisation.
        self.assertEqual(rlr.shin_pair(0.5, 0.45), (0.5 / 0.95, 1 - 0.5 / 0.95))

    def test_prices(self):
        self.assertIsNone(rlr.american_to_decimal(50))
        self.assertIsNone(rlr.american_to_decimal(-99.5))
        self.assertIsNone(rlr.american_to_decimal(None))
        self.assertEqual(rlr.american_to_decimal(100), 2.0)
        self.assertEqual(rlr.american_to_decimal(-100), 2.0)
        self.assertEqual(rlr.american_to_decimal(-200), 1.5)

    def test_confidence_sequence_radius_at_1000(self):
        rho = 1000 / (2 * math.log(20) + math.log(1 + 2 * math.log(20)))
        self.assertAlmostEqual(rlr.RHO, rho, places=12)
        self.assertAlmostEqual(rho, 126.0, delta=0.01)
        v = 1000 + rho
        by_hand = math.sqrt(v * math.log(v / (rho * 0.05 ** 2))) / 1000
        self.assertAlmostEqual(rlr.cs_radius(1000), by_hand, places=12)
        self.assertAlmostEqual(rlr.cs_radius(1000), 0.096, delta=0.0005)

    def test_calibration_edges(self):
        self.assertIsNone(rlr.cal_group(49.99))
        self.assertEqual(rlr.CAL_NAMES[rlr.cal_group(50.0)], "50-55")
        self.assertEqual(rlr.CAL_NAMES[rlr.cal_group(54.99)], "50-55")
        self.assertEqual(rlr.CAL_NAMES[rlr.cal_group(55.0)], "55-60")
        self.assertEqual(rlr.CAL_NAMES[rlr.cal_group(85.0)], "85-100")
        self.assertEqual(rlr.CAL_NAMES[rlr.cal_group(100.0)], "85-100")

    def test_bootstrap_is_seeded_per_figure(self):
        vals = [0.3, -0.1, 0.25, 0.0, 0.7]
        a, b = rlr.bootstrap95(vals), rlr.bootstrap95(vals)
        self.assertEqual(a, b)
        self.assertTrue(min(vals) <= a[0] <= a[1] <= max(vals))
        self.assertEqual(rlr.bootstrap95([1.0]), [1.0, 1.0])
        self.assertEqual(rlr.bootstrap95([]), [None, None])


class ThreeGameWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.odds, cls.team = build_three_game_dbs(cls.tmp.name)
        cls.out = rlr.reconcile(cls.odds, cls.team)
        cls.w = {w["key"]: w for w in cls.out["windows"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counted(self):
        c = self.out["counted"]
        self.assertEqual(c["rows_total"], 5)
        self.assertEqual(c["excluded"], {"other_sport": 0, "other_book": 1, "simulated": 0, "duplicate_later_log": 1})
        self.assertEqual((c["games"], c["graded"]), (3, 3))
        self.assertEqual((c["ungraded_ids"], c["unconfirmed_ids"], c["conflicts"]), ([], [], []))

    def test_windows_present(self):
        self.assertEqual(sorted(self.w), [f"2026-27|all|{M}", f"2026-27|regular_season|{M}",
                                          f"all-time|all|{M}", f"all-time|regular_season|{M}"])
        self.assertEqual([w["key"] for w in self.out["windows"]], sorted(self.w))

    def test_figures_by_hand(self):
        w = self.w[f"2026-27|all|{M}"]
        self.assertEqual(w["n"], 3)
        # Picks: G1 BOS (won), G2 MIA (LAC won), G3 BOS (won); counted G3 row is id 4, not id 5.
        self.assertEqual((w["hit"]["k"], w["hit"]["n"]), (2, 3))
        self.assertEqual(w["hit"]["wilson95"], rlr.wilson(2, 3))
        self.assertIsNone(w["hit_league_results"])
        b = w["baselines"]
        self.assertEqual((b["home"]["k"], b["home"]["n"]), (2, 3))                 # y = 0, 1, 1
        # Records before each date: G1 NYK 1-0 = BOS 1-0 -> home (miss); G2 LAC 0-2 < MIA 1-1 -> MIA
        # (miss); G3 BOS 2-0 > NYK 1-1 -> BOS (hit).
        self.assertEqual((b["better_record"]["k"], b["better_record"]["n"]), (1, 3))
        # Logged favourites: BOS (-150), LAC (-120), pick'em -> home BOS: all three won.
        self.assertEqual((b["favorite_logged"]["k"], b["favorite_logged"]["n"]), (3, 3))
        # Close (priced only: G1, G3): G1 BOS -160 (won); G3 NYK -115 (lost).
        self.assertEqual((b["favorite_close"]["k"], b["favorite_close"]["n"]), (1, 2))

        y = [0, 1, 1]
        p_model = [0.4, 0.45, 0.7]
        p1 = (1 / 2.3) / (1 / 2.3 + 0.6)
        p2 = (120 / 220) / (120 / 220 + 0.5)
        p3 = 0.5
        p_mkt = [p1, p2, p3]
        brier = lambda ps: sum((p - t) ** 2 for p, t in zip(ps, y)) / 3
        ll_one = lambda p, t: -math.log(p) if t else -math.log(1 - p)
        pr = w["probability"]
        self.assertEqual((pr["n"], pr["missing_prices"]), (3, 0))
        self.assertAlmostEqual(pr["model"]["brier"], (0.16 + 0.3025 + 0.09) / 3, places=15)
        self.assertAlmostEqual(pr["model"]["log_loss"], (-math.log(0.6) - math.log(0.45) - math.log(0.7)) / 3, places=15)
        self.assertAlmostEqual(pr["market"]["proportional"]["brier"], brier(p_mkt), places=15)
        self.assertAlmostEqual(pr["market"]["proportional"]["log_loss"],
                               sum(ll_one(p, t) for p, t in zip(p_mkt, y)) / 3, places=15)
        shin = []
        for ph, pa in ((1 / 2.3, 0.6), (120 / 220, 0.5), (110 / 210, 110 / 210)):
            beta = ph + pa
            shin.append(0.5 + (ph * ph - pa * pa) / (2 * beta))
        self.assertAlmostEqual(pr["market"]["shin"]["brier"], brier(shin), places=12)
        self.assertAlmostEqual(pr["brier_skill_vs_market"], 1 - pr["model"]["brier"] / brier(p_mkt), places=15)

        cl = pr["close"]
        self.assertEqual(cl["n"], 2)
        self.assertAlmostEqual(cl["model"]["brier"], (0.16 + 0.09) / 2, places=15)
        c1 = (1 / 2.4) / (1 / 2.4 + 160 / 260)
        c3 = (105 / 205) / (105 / 205 + 115 / 215)
        self.assertAlmostEqual(cl["market"]["proportional"]["brier"], ((c1 - 0) ** 2 + (c3 - 1) ** 2) / 2, places=15)

        d = [ll_one(pm, t) - ll_one(pk, t) for pm, pk, t in zip(p_model, p_mkt, y)]
        md = sum(d) / 3
        sd = math.sqrt(sum((x - md) ** 2 for x in d) / 2)
        pl = w["paired_log_loss"]
        self.assertEqual(pl["n"], 3)
        self.assertAlmostEqual(pl["mean_d"], md, places=15)
        self.assertAlmostEqual(pl["normal95"][0], md - Z * sd / math.sqrt(3), places=14)
        self.assertAlmostEqual(pl["normal95"][1], md + Z * sd / math.sqrt(3), places=14)
        self.assertTrue(min(d) - 1e-15 <= pl["bootstrap95"][0] <= pl["bootstrap95"][1] <= max(d) + 1e-15)

        cal = {g["group"]: g for g in w["calibration"]}
        self.assertEqual([g["group"] for g in w["calibration"]], rlr.CAL_NAMES)
        self.assertEqual((cal["55-60"]["n"], cal["55-60"]["mean_stated"], cal["55-60"]["hit_rate"]), (1, 55.0, 0.0))
        self.assertEqual((cal["60-65"]["n"], cal["60-65"]["mean_stated"], cal["60-65"]["hit_rate"]), (1, 60.0, 1.0))
        self.assertEqual((cal["70-75"]["n"], cal["70-75"]["mean_stated"], cal["70-75"]["hit_rate"]), (1, 70.0, 1.0))
        self.assertEqual((cal["50-55"]["n"], cal["50-55"]["mean_stated"], cal["50-55"]["wilson95"]), (0, None, [None, None]))

        clv = w["clv"]
        self.assertEqual(clv["same_book"], {"n": 1, "mean_pts": 1.0, "bootstrap95_pts": [1.0, 1.0], "share_positive": 1.0})
        self.assertEqual(clv["consensus"], {"n": 1, "mean_pts": 2.0, "bootstrap95_pts": [2.0, 2.0], "share_positive": 1.0})
        self.assertEqual(clv["reconstructed"], {"consensus_n": 0, "same_book_n": 1})
        self.assertEqual(clv["status_counts"], {"close_too_early": 1, "priced": 2})

        seq = w["sequential"]
        r3, r2 = rlr.cs_radius(3), rlr.cs_radius(2)
        self.assertEqual(seq["home"], {"n": 3, "mean": 0.0, "cs95": [-r3, r3], "verdict": "not_yet"})      # x = 1, -1, 0
        self.assertAlmostEqual(seq["better_record"]["mean"], 1 / 3, places=15)                         # x = 1, 0, 0
        self.assertAlmostEqual(seq["favorite_logged"]["mean"], -1 / 3, places=15)                       # x = 0, -1, 0
        self.assertEqual((seq["favorite_close"]["n"], seq["favorite_close"]["mean"]), (2, 0.5))         # x = 0, 1
        self.assertAlmostEqual(seq["favorite_close"]["cs95"][1], 0.5 + r2, places=15)
        mb = [(pk - t) ** 2 - (pm - t) ** 2 for pm, pk, t in zip(p_model, p_mkt, y)]
        self.assertAlmostEqual(seq["market_brier"]["mean"], sum(mb) / 3, places=15)
        self.assertGreater(r3, 1.0)  # three games can never move a verdict
        for s in seq.values():
            self.assertEqual(s["verdict"], "not_yet")

    def test_profit_by_hand(self):
        w = self.w[f"2026-27|all|{M}"]
        # Our side: G1 BOS away -150 (won), G2 MIA away +100 (lost), G3 BOS home -110 (won).
        dec = [1 + 100 / 150, 2.0, 1 + 100 / 110]
        units = [dec[0] - 1, -1.0, dec[2] - 1]
        lg = w["profit"]["logged"]
        self.assertEqual((lg["n"], lg["missing_price"]), (3, 0))
        self.assertAlmostEqual(lg["units"], sum(units), places=14)
        self.assertAlmostEqual(lg["per_unit"], sum(units) / 3, places=14)
        self.assertAlmostEqual(lg["break_even_rate"], (0.6 + 0.5 + 110 / 210) / 3, places=14)
        self.assertAlmostEqual(lg["hit_rate"], 2 / 3, places=15)
        self.assertTrue(min(units) <= lg["bootstrap95"][0] <= lg["bootstrap95"][1] <= max(units))
        # Close, priced games only: G1 BOS -160 (won), G3 BOS -105 (won); G2 is close_too_early.
        cl = w["profit"]["close"]
        self.assertEqual(cl["n"], 2)
        self.assertAlmostEqual(cl["units"], 100 / 160 + 100 / 105, places=14)
        self.assertAlmostEqual(cl["per_unit"], (100 / 160 + 100 / 105) / 2, places=14)
        be = w["sequential"]["break_even"]
        x = [1 - 0.6, 0 - 0.5, 1 - 110 / 210]
        self.assertEqual(be["n"], 3)
        self.assertAlmostEqual(be["mean"], sum(x) / 3, places=14)
        self.assertEqual(be["verdict"], "not_yet")

    def test_integrity_clean(self):
        i = self.out["integrity"]
        self.assertEqual(i["logged_at_not_before_tip_ids"], [])
        self.assertEqual(i["id_gaps"], [])
        self.assertEqual(i["winner_differs_from_league"], [])
        self.assertEqual(i["winner_differs_from_espn"], [])
        self.assertEqual(i["checks_run"]["graded_rows_checked_against_league"], 5)
        self.assertEqual(i["checks_run"]["graded_rows_checked_against_espn"], 1)


class TwoPickProfit(unittest.TestCase):
    """A win at -150 and a loss at +130: units 2/3 - 1, per unit -1/6, break-even (0.6 + 1/2.3)/2."""

    def test_two_picks(self):
        win = dict(LEDGER_ROWS[0])            # BOS at -150, won; priced close BOS -160
        loss = dict(LEDGER_ROWS[2])           # MIA, lost
        loss.update(home_ml=-150.0, away_ml=130.0, clv_status=None, closing_home_ml=None, closing_away_ml=None)
        with tempfile.TemporaryDirectory() as d:
            odds, team = build_three_game_dbs(d, [win, loss])
            out = rlr.reconcile(odds, team)
        w = {x["key"]: x for x in out["windows"]}[f"2026-27|all|{M}"]
        lg = w["profit"]["logged"]
        self.assertEqual((lg["n"], lg["missing_price"]), (2, 0))
        self.assertAlmostEqual(lg["units"], -1 / 3, places=14)
        self.assertAlmostEqual(lg["per_unit"], -1 / 6, places=14)
        self.assertAlmostEqual(lg["break_even_rate"], (0.6 + 1 / 2.3) / 2, places=14)
        self.assertAlmostEqual(lg["break_even_rate"], 0.517391, places=6)
        self.assertEqual(lg["hit_rate"], 0.5)
        lo, hi = lg["bootstrap95"]
        self.assertTrue(-1.0 <= lo <= -1 / 6 <= hi <= 2 / 3 + 1e-15)
        self.assertEqual(w["profit"]["close"]["n"], 1)
        self.assertAlmostEqual(w["profit"]["close"]["units"], 0.625, places=15)
        self.assertEqual(w["profit"]["close"]["bootstrap95"], [0.625, 0.625])
        be = w["sequential"]["break_even"]
        self.assertAlmostEqual(be["mean"], ((1 - 0.6) + (0 - 1 / 2.3)) / 2, places=14)

    def test_missing_price_is_left_out_and_counted(self):
        row = dict(LEDGER_ROWS[0])
        row.update(away_ml=-50.0)             # our side's price is not a valid American price
        with tempfile.TemporaryDirectory() as d:
            odds, team = build_three_game_dbs(d, [row])
            out = rlr.reconcile(odds, team)
        w = out["windows"][0]
        lg = w["profit"]["logged"]
        self.assertEqual((lg["n"], lg["missing_price"], lg["units"], lg["per_unit"]), (0, 1, None, None))
        self.assertEqual((lg["bootstrap95"], lg["break_even_rate"], lg["hit_rate"]), ([None, None], None, None))
        self.assertEqual(w["sequential"]["break_even"],
                         {"n": 0, "mean": None, "cs95": [None, None], "verdict": "not_yet"})


def dump_endpoint_json(odds_path: str, out_path: str, where: str = "1=1") -> None:
    """predictions_log rows as the public /api/prediction-log returns them: a dict with a
    "predictions" list of row dicts, each with an extra "season" field."""
    con = sqlite3.connect(odds_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"SELECT * FROM predictions_log WHERE {where} ORDER BY id")]
    con.close()
    for r in rows:
        r["season"] = "2026-27"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"season": "2026-27", "predictions": rows}, fh)


def run_main(args: list[str], out_path: str) -> dict:
    rlr.main(args + ["--out", out_path])
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


class LedgerJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.odds, self.team = build_three_game_dbs(self.d)

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, name):
        return os.path.join(self.d, name)

    def test_round_trip_identical(self):
        dump_endpoint_json(self.odds, self.p("log.json"))
        a = run_main(["--ledger", self.odds, "--league", self.team], self.p("a.json"))
        b = run_main(["--ledger-json", self.p("log.json"), "--league", self.team], self.p("b.json"))
        self.assertEqual(a, b)

    def test_several_inputs_deduplicated_by_id(self):
        dump_endpoint_json(self.odds, self.p("one.json"), "id <= 3")
        dump_endpoint_json(self.odds, self.p("two.json"), "id >= 2")   # ids 2 and 3 appear twice
        a = run_main(["--ledger", self.odds, "--league", self.team], self.p("a.json"))
        b = run_main(["--ledger-json", self.p("one.json"), "--ledger-json", self.p("two.json"),
                      "--league", self.team], self.p("b.json"))
        self.assertEqual(a, b)

    def test_sealed_row_counts_as_ungraded(self):
        dump_endpoint_json(self.odds, self.p("log.json"))
        with open(self.p("log.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        sealed = {k: None for k in doc["predictions"][0]}
        sealed.update(id=6, logged_at="2026-10-28T14:00:00Z", log_date="2026-10-28", sport="NBA",
                      sportsbook="fanduel", game_key="Miami Heat:Boston Celtics", home_team="Miami Heat",
                      away_team="Boston Celtics", game_start_time_utc="2026-10-28T23:30:00Z",
                      season="2026-27", sealed_until_tipoff=True)
        doc["predictions"].append(sealed)
        with open(self.p("log.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        out = run_main(["--ledger-json", self.p("log.json"), "--league", self.team], self.p("b.json"))
        self.assertEqual((out["counted"]["games"], out["counted"]["graded"], out["counted"]["ungraded_ids"]), (4, 3, [6]))
        self.assertEqual(out["windows"][0]["hit"]["k"], 2)
        self.assertTrue(any("sealed" in n for n in out["notes"]))

    def test_without_league(self):
        dump_endpoint_json(self.odds, self.p("log.json"))
        out = run_main(["--ledger-json", self.p("log.json")], self.p("b.json"))
        self.assertTrue(out["notes"] and "better-record" in out["notes"][0])
        self.assertEqual(out["counted"]["unconfirmed_ids"], [3])        # the ESPN-graded game has no id
        self.assertEqual(out["counted"]["excluded"]["duplicate_later_log"], 1)
        w = {x["key"]: x for x in out["windows"]}[f"2026-27|all|{M}"]
        self.assertEqual((w["hit"]["k"], w["hit"]["n"]), (2, 3))
        self.assertEqual(w["baselines"]["better_record"], {"k": None, "n": 0, "rate": None})
        self.assertEqual(w["sequential"]["better_record"]["verdict"], "not_yet")
        self.assertIsNone(w["sequential"]["better_record"]["mean"])
        self.assertEqual(w["baselines"]["home"]["k"], 2)
        self.assertFalse(out["integrity"]["checks_run"]["league_given"])

    @unittest.skipUnless(os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Tests",
                                                     "live_eval_fixture.py")), "synthetic fixture not present")
    def test_round_trip_on_synthetic_fixture(self):
        import subprocess
        gen = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Tests", "live_eval_fixture.py")
        fx = self.p("fixture")
        subprocess.run([sys.executable, "-I", gen, fx], check=True, capture_output=True)
        odds, team = os.path.join(fx, "OddsData.sqlite"), os.path.join(fx, "TeamData.sqlite")
        dump_endpoint_json(odds, self.p("log.json"))
        a = run_main(["--ledger", odds, "--league", team], self.p("a.json"))
        b = run_main(["--ledger-json", self.p("log.json"), "--league", team], self.p("b.json"))
        self.assertEqual(a, b)
        self.assertGreater(len(a["windows"]), 10)


class Commitments(unittest.TestCase):
    """A hand-built bb-ledger-commit-v1 example (no published reveal existed to test against)."""

    NONCE1, NONCE2 = "ab" * 32, "0123456789abcdef" * 4
    COMMITTED1, COMMITTED2 = "2026-10-25T15:00:05Z", "2026-10-26T14:00:05Z"

    # Preimages typed out from the README's recipe.
    PRE1 = ('["bb-ledger-commit-v1","2026-10-25",1,"' + "ab" * 32 + '",[[1,"2026-10-25","NBA","fanduel",'
            '"New York Knicks:Boston Celtics","2026-10-25T23:30:00Z","2026-10-25T15:00:00Z","candidate_2026-08",'
            '"Boston Celtics","60.000000","130.000000","-150.000000"]]]')
    PRE2 = ('["bb-ledger-commit-v1","2026-10-26",1,"' + "0123456789abcdef" * 4 + '",[[3,"2026-10-26","NBA","fanduel",'
            '"LA Clippers:Miami Heat","2026-10-27T01:00:00Z","2026-10-26T14:00:00Z","candidate_2026-08",'
            '"Miami Heat","55.000000","-120.000000","100.000000"],[4,"2026-10-26","NBA","fanduel",'
            '"Boston Celtics:New York Knicks","2026-10-28T00:00:00Z","2026-10-26T13:00:00Z","candidate_2026-08",'
            '"Boston Celtics","70.000000","-110.000000","-110.000000"]]]')

    @staticmethod
    def h(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.odds, self.team = build_three_game_dbs(self.tmp.name)
        p1, p2 = self.h(self.PRE1), self.h(self.PRE2)
        c1 = self.h(f'["bb-ledger-commit-v1","chain","{"0" * 64}","2026-10-25",1,1,"{self.COMMITTED1}","{p1}"]')
        c2 = self.h(f'["bb-ledger-commit-v1","chain","{c1}","2026-10-26",1,2,"{self.COMMITTED2}","{p2}"]')
        self.lines = [
            {"log_date": "2026-10-25", "seq": 1, "n_picks": 1, "pick_ids": [1], "committed_at": self.COMMITTED1,
             "first_tipoff_utc": "2026-10-25T23:30:00Z", "picks_sha256": p1, "prev_chain_sha256": "0" * 64,
             "chain_sha256": c1, "method_version": "bb-ledger-commit-v1"},
            {"log_date": "2026-10-26", "seq": 1, "n_picks": 2, "pick_ids": [3, 4], "committed_at": self.COMMITTED2,
             "first_tipoff_utc": "2026-10-27T01:00:00Z", "picks_sha256": p2, "prev_chain_sha256": c1,
             "chain_sha256": c2, "method_version": "bb-ledger-commit-v1"},
        ]
        by_id = {i + 1: r for i, r in enumerate(LEDGER_ROWS)}
        pick = lambda i: {"id": i, **{f: by_id[i].get(f) for f in rlr.PICK_FIELDS if f != "id"}}
        self.reveals = {"commitments": [
            {**self.lines[0], "nonce": self.NONCE1, "picks": [pick(1)]},
            {**self.lines[1], "nonce": self.NONCE2, "picks": [pick(4), pick(3)]},  # order must not matter
        ]}

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, lines, reveals):
        cp, rp = os.path.join(self.tmp.name, "c.jsonl"), os.path.join(self.tmp.name, "r.json")
        with open(cp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
        with open(rp, "w", encoding="utf-8") as fh:
            json.dump(reveals, fh)
        out = os.path.join(self.tmp.name, "out.json")
        rlr.main(["--ledger", self.odds, "--league", self.team, "--commitments", cp, "--reveals", rp, "--out", out])
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)["integrity"]["commitments"]

    def test_round_trip(self):
        self.assertEqual(rlr.picks_sha256("2026-10-25", 1, self.NONCE1, self.reveals["commitments"][0]["picks"]),
                         self.h(self.PRE1))
        c = self.run_check(self.lines, self.reveals)
        self.assertEqual((c["lines"], c["reveals_checked"], c["picks_hash_matches"]), (2, 2, 2))
        for k in ("chain_breaks", "chain_hash_mismatches", "line_problems", "picks_hash_mismatches",
                  "pick_id_mismatches", "pick_field_mismatches", "picks_not_in_ledger"):
            self.assertEqual(c[k], [], k)

    def test_tampering_is_caught(self):
        rv = json.loads(json.dumps(self.reveals))
        rv["commitments"][1]["picks"][1]["predicted_winner"] = "LA Clippers"   # row 3 changed
        lines = json.loads(json.dumps(self.lines))
        lines[1]["prev_chain_sha256"] = "f" * 64
        c = self.run_check(lines, rv)
        self.assertEqual(len(c["picks_hash_mismatches"]), 1)
        self.assertEqual(c["pick_field_mismatches"],
                         [{"id": 3, "field": "predicted_winner", "revealed": "LA Clippers", "ledger": "Miami Heat"}])
        self.assertEqual(len(c["chain_breaks"]), 1)
        self.assertEqual(len(c["chain_hash_mismatches"]), 1)  # chain hash covers prev, so it no longer matches


if __name__ == "__main__":
    unittest.main(verbosity=2)
