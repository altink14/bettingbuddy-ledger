#!/usr/bin/env python3
"""
reconcile_live_record.py
========================
An independent recomputation of Betting Buddy's live NBA pick record under
the method bb-nba-live-eval-v1 and its appendix "the exact arithmetic".

Where the documents live:
  - the plan:        public  github.com/altink14/bettingbuddy-ledger  nba/EVALUATION_PLAN_v1.md
                     backend docs/ledger/EVALUATION_PLAN_v1.md
  - the output spec: public  nba/audit/OUTPUT.md
                     backend docs/ledger/live_eval_output.md
  - this script:     public  nba/audit/reconcile_live_record.py
                     backend audit/reconcile_live_record.py

It was written from the plan alone, without reading the site's own scorer,
so that agreement between the two outputs means something. It reads the
prediction ledger and the league's game log, recomputes every figure, and
prints the JSON of the output spec, plus "integrity" and "notes" sections that
only this checker emits.

Requirements: Python 3.10+ and the standard library only. On Windows the
America/New_York time zone needs the IANA database: `pip install tzdata`.

From the backend's databases:

    python reconcile_live_record.py --ledger OddsData.sqlite \\
        --league TeamData.sqlite [--out record.json] \\
        [--commitments commitments.jsonl --reveals reveals.json]

From public data only (one --ledger-json per season; a URL or a saved file):

    python reconcile_live_record.py \\
        --ledger-json "https://nba-machine-learning-sports-betting-production-499f.up.railway.app/api/prediction-log?season=2026-27"

Without --league, the better-record baseline cannot be computed and games
graded from ESPN's final get no official game id (they are "unconfirmed");
the output's "notes" says so. Databases are opened read-only. Nothing is
written except --out.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception as exc:  # pragma: no cover - environment problem, not a figure
    sys.exit(f"America/New_York time zone unavailable ({exc}); on Windows run: pip install tzdata")

# ---------------------------------------------------------------------------
# Constants fixed by the plan
# ---------------------------------------------------------------------------
METHOD = "bb-nba-live-eval-v1"
COUNTED_SPORT = "NBA"
COUNTED_BOOK = "fanduel"
SIMULATED_MODEL = "implied_probability_sim"

Z95 = 1.959963984540054
CLIP_LO, CLIP_HI = 1e-6, 1.0 - 1e-6
SEED = 20261020
BOOT_B = 10_000
BOOT_LO_INDEX, BOOT_HI_INDEX = 250, 9749

ALPHA = 0.05
SIGMA2 = 1.0
RHO = 1000.0 / (2.0 * math.log(1.0 / ALPHA) + math.log(1.0 + 2.0 * math.log(1.0 / ALPHA)))

SHIN_HI = 1.0 - 1e-12
SHIN_MAX_HALVINGS = 200
SHIN_MIN_WIDTH = 1e-15

CAL_EDGES = [50, 55, 60, 65, 70, 75, 80, 85]
CAL_NAMES = ["50-55", "55-60", "60-65", "65-70", "70-75", "75-80", "80-85", "85-100"]

TYPE_BY_PREFIX = {
    "002": "regular_season",
    "005": "play_in",
    "004": "playoffs",
    "006": "cup_final",
    "001": "preseason",  # never logged per the plan; labelled if it ever appears
}
UNCONFIRMED = "unconfirmed"

# "LA Clippers" is the Los Angeles Clippers (appendix, "Matching a game").
TEAM_ALIASES = {"la clippers": "los angeles clippers"}

COMMIT_METHOD = "bb-ledger-commit-v1"
PICK_FIELDS = [
    "id", "log_date", "sport", "sportsbook", "game_key", "game_start_time_utc",
    "logged_at", "model", "predicted_winner", "winner_confidence", "home_ml", "away_ml",
]
NUMERIC_PICK_FIELDS = {"winner_confidence", "home_ml", "away_ml"}
ZERO_HASH = "0" * 64

GAME_ID_RE = re.compile(r"^\d{10}$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def open_ro(path: str) -> sqlite3.Connection:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"not a file: {path}")
    con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def parse_utc(s) -> datetime | None:
    """ISO-8601 text to an aware UTC datetime; text without an offset is read as UTC."""
    if blank(s):
        return None
    t = str(s).strip()
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day(s) -> date | None:
    if blank(s):
        return None
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return None


def eastern_date(dt: datetime) -> date:
    return dt.astimezone(EASTERN).date()


def season_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def season_from_date(d: date) -> str:
    """A season runs from 1 October to 30 September."""
    return season_label(d.year if d.month >= 10 else d.year - 1)


def season_yy_from_label(label: str) -> str:
    return label[2:4]


def season_from_id(gid: str) -> str:
    # Characters 4-5 of the id: 0022600001 -> "26" -> 2026-27.
    return season_label(2000 + int(gid[3:5]))


def type_from_id(gid: str) -> str:
    return TYPE_BY_PREFIX.get(gid[:3], f"other_{gid[:3]}")


def norm_game_id(v) -> str | None:
    if blank(v):
        return None
    s = str(v).strip()
    if s.isdigit() and len(s) < 10:
        s = s.zfill(10)
    return s if GAME_ID_RE.match(s) else None


def to_float(v) -> float | None:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def rate(k: int, n: int) -> float | None:
    return k / n if n else None


def clip(p: float) -> float:
    return min(max(p, CLIP_LO), CLIP_HI)


# ---------------------------------------------------------------------------
# The arithmetic of the appendix
# ---------------------------------------------------------------------------
def american_to_decimal(x) -> float | None:
    """x >= 100 -> 1 + x/100; x <= -100 -> 1 + 100/(-x); anything else is missing."""
    x = to_float(x)
    if x is None:
        return None
    if x >= 100:
        return 1.0 + x / 100.0
    if x <= -100:
        return 1.0 + 100.0 / (-x)
    return None


def implied(x) -> float | None:
    d = american_to_decimal(x)
    return None if d is None else 1.0 / d


def proportional(pi_h: float, pi_a: float) -> float:
    return pi_h / (pi_h + pi_a)


def _shin_p(z: float, pi: float, beta: float) -> float:
    return (math.sqrt(z * z + 4.0 * (1.0 - z) * pi * pi / beta) - z) / (2.0 * (1.0 - z))


def shin_z(pi_h: float, pi_a: float) -> float | None:
    """Shin's z by bisection on [0, 1 - 1e-12]; None when beta <= 1 (no margin)."""
    beta = pi_h + pi_a
    if beta <= 1.0:
        return None
    lo, hi = 0.0, SHIN_HI
    for _ in range(SHIN_MAX_HALVINGS):
        if hi - lo < SHIN_MIN_WIDTH:
            break
        mid = (lo + hi) / 2.0
        if _shin_p(mid, pi_h, beta) + _shin_p(mid, pi_a, beta) - 1.0 > 0.0:
            lo = mid  # sum still above 1: z must grow
        else:
            hi = mid
    return (lo + hi) / 2.0


def shin_pair(pi_h: float, pi_a: float) -> tuple[float, float]:
    """No-margin (home, away) probabilities by Shin's method, two outcomes."""
    beta = pi_h + pi_a
    z = shin_z(pi_h, pi_a)
    if z is None:
        p = proportional(pi_h, pi_a)
        return p, 1.0 - p
    return _shin_p(z, pi_h, beta), _shin_p(z, pi_a, beta)


def shin_home(pi_h: float, pi_a: float) -> float:
    return shin_pair(pi_h, pi_a)[0]


def wilson(k: int, n: int) -> list:
    if n <= 0:
        return [None, None]
    z = Z95
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return [center - half, center + half]


def brier_one(p: float, y: int) -> float:
    return (p - y) ** 2


def log_loss_one(p: float, y: int) -> float:
    q = clip(p)
    return -(y * math.log(q) + (1 - y) * math.log(1.0 - q))


def bootstrap95(values: list[float]) -> list:
    """Fresh Random(20261020); 10,000 resamples of n draws; [means[250], means[9749]]."""
    n = len(values)
    if n == 0:
        return [None, None]
    rng = random.Random(SEED)
    means = []
    for _ in range(BOOT_B):
        idx = [rng.randrange(n) for _ in range(n)]
        means.append(sum(values[i] for i in idx) / n)
    means.sort()
    return [means[BOOT_LO_INDEX], means[BOOT_HI_INDEX]]


def normal95(values: list[float]) -> list:
    n = len(values)
    if n < 2:
        return [None, None]
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))
    half = Z95 * sd / math.sqrt(n)
    return [m - half, m + half]


def cs_radius(n: int) -> float:
    """Half-width of the two-sided normal-mixture confidence sequence after n games."""
    v = n * SIGMA2 + RHO
    return math.sqrt(v * math.log(v / (RHO * ALPHA * ALPHA))) / n


def sequential(xs: list[float]) -> dict:
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "cs95": [None, None], "verdict": "not_yet"}
    m = sum(xs) / n
    r = cs_radius(n)
    lo, hi = m - r, m + r
    verdict = "ahead" if lo > 0 else ("behind" if hi < 0 else "not_yet")
    return {"n": n, "mean": m, "cs95": [lo, hi], "verdict": verdict}


def cal_group(c: float) -> int | None:
    """Index of the group whose lower edge is the largest of 50..85 not above c."""
    if c < 50:
        return None
    gi = None
    for i, edge in enumerate(CAL_EDGES):
        if edge <= c:
            gi = i
    return gi


# ---------------------------------------------------------------------------
# The league's data
# ---------------------------------------------------------------------------
class League:
    def __init__(self, path: str | None):
        """path None: no league data (public-data run). Nothing matches and no
        records exist; callers check `available`."""
        self.available = path is not None
        self.team_by_name: dict[str, int] = {}
        self.name_by_team: dict[int, str] = {}
        self.games: dict[str, dict] = {}
        self.pair_index: dict[frozenset, list[tuple[date, str]]] = {}
        self.records: dict[tuple[str, int], tuple[list[date], list[int]]] = {}
        self.has_espn = False
        self.espn_by_id: dict[str, dict] = {}
        self.espn_pairs: dict[frozenset, list[tuple[date, str]]] = {}
        if path is None:
            return
        con = open_ro(path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for r in con.execute("SELECT team_id, full_name FROM team_metadata"):
            if r["full_name"] is None:
                continue
            self.team_by_name[str(r["full_name"]).strip().lower()] = int(r["team_id"])
            self.name_by_team[int(r["team_id"])] = str(r["full_name"])

        # game id -> {"date", "teams", "home", "away", "winner"}
        self.games: dict[str, dict] = {}
        team_rows: dict[tuple[str, int], list[tuple[date, bool]]] = {}
        for r in con.execute("SELECT game_id, team_id, game_date, matchup, wl FROM game_results"):
            gid = norm_game_id(r["game_id"])
            if gid is None or r["team_id"] is None:
                continue
            tid = int(r["team_id"])
            gd = parse_day(r["game_date"])
            g = self.games.setdefault(gid, {"date": gd, "teams": set(), "home": None, "away": None, "winner": None})
            if gd is not None and (g["date"] is None or gd < g["date"]):
                g["date"] = gd
            g["teams"].add(tid)
            matchup = r["matchup"] or ""
            if "vs." in matchup:
                g["home"] = tid
            elif "@" in matchup:
                g["away"] = tid
            wl = (r["wl"] or "").strip().upper()
            if wl == "W":
                g["winner"] = tid
            # Regular-season games (id prefix 002), per season (id chars 4-5), per team.
            if gid.startswith("002") and wl in ("W", "L") and gd is not None:
                team_rows.setdefault((gid[3:5], tid), []).append((gd, wl == "W"))

        self.pair_index: dict[frozenset, list[tuple[date, str]]] = {}
        for gid, g in self.games.items():
            if len(g["teams"]) == 2 and g["date"] is not None:
                self.pair_index.setdefault(frozenset(g["teams"]), []).append((g["date"], gid))

        # Cumulative regular-season records: dates ascending, wins before each index.
        self.records: dict[tuple[str, int], tuple[list[date], list[int]]] = {}
        for key, games in team_rows.items():
            games.sort()
            dates, cum = [], [0]
            for d, won in games:
                dates.append(d)
                cum.append(cum[-1] + (1 if won else 0))
            self.records[key] = (dates, cum)

        self.has_espn = "espn_box_scores" in tables
        self.espn_by_id: dict[str, dict] = {}
        self.espn_pairs: dict[frozenset, list[tuple[date, str]]] = {}
        if self.has_espn:
            for r in con.execute(
                "SELECT espn_event_id, game_date, home_team_id, away_team_id, home_pts, away_pts FROM espn_box_scores"
            ):
                eid = str(r["espn_event_id"]).strip()
                rec = {
                    "date": parse_day(r["game_date"]),
                    "home": r["home_team_id"], "away": r["away_team_id"],
                    "home_pts": r["home_pts"], "away_pts": r["away_pts"],
                }
                self.espn_by_id[eid] = rec
                if rec["home"] is not None and rec["away"] is not None and rec["date"] is not None:
                    self.espn_pairs.setdefault(frozenset((int(rec["home"]), int(rec["away"]))), []).append((rec["date"], eid))
        con.close()

    def team_id(self, name) -> int | None:
        if blank(name):
            return None
        k = str(name).strip().lower()
        k = TEAM_ALIASES.get(k, k)
        return self.team_by_name.get(k)

    @staticmethod
    def _nearest(cands: list[tuple[date, str]], d: date) -> str | None:
        best = None
        for gd, gid in cands:
            dist = abs((gd - d).days)
            if dist <= 1:
                key = (dist, gd, gid)  # nearest date, then the earlier, then the lower id
                if best is None or key < best:
                    best = key
        return None if best is None else best[2]

    def match_game(self, home_id, away_id, d: date | None) -> str | None:
        if home_id is None or away_id is None or d is None:
            return None
        return self._nearest(self.pair_index.get(frozenset((home_id, away_id)), []), d)

    def match_espn(self, ref, home_id, away_id, d: date | None) -> str | None:
        if not self.has_espn:
            return None
        if not blank(ref) and str(ref).strip() in self.espn_by_id:
            return str(ref).strip()
        if home_id is None or away_id is None or d is None:
            return None
        return self._nearest(self.espn_pairs.get(frozenset((home_id, away_id)), []), d)

    def record_before(self, season_yy: str, team: int, d: date) -> tuple[int, int]:
        """(wins, games) in that season's regular-season games dated before d."""
        rec = self.records.get((season_yy, team))
        if rec is None:
            return 0, 0
        dates, cum = rec
        g = bisect.bisect_left(dates, d)
        return cum[g], g


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
def load_ledger(path: str) -> list[dict]:
    con = open_ro(path)
    rows = [dict(r) for r in con.execute("SELECT * FROM predictions_log ORDER BY id")]
    con.close()
    return rows


JSON_ONLY_FIELDS = {"season", "sealed_until_tipoff"}  # endpoint extras, not ledger columns
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_json_source(src: str):
    if re.match(r"^https?://", src, re.I):
        import urllib.request

        req = urllib.request.Request(src, headers={"User-Agent": "bb-reconcile-live-record/1", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    with open(src, encoding="utf-8") as fh:
        return json.load(fh)


def load_ledger_json(sources: list[str]) -> tuple[list[dict], set[int], list[str]]:
    """Rows of the public /api/prediction-log JSON (one source per season, URL or
    file), concatenated, de-duplicated by id, loaded into an in-memory
    predictions_log and read back exactly as load_ledger reads the database."""
    by_id: dict[int, dict] = {}
    sealed: set[int] = set()
    notes: list[str] = []
    differing: list[int] = []
    for src in sources:
        doc = _read_json_source(src)
        rows = doc.get("predictions") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise SystemExit(f"{src}: expected a JSON object with a 'predictions' list")
        for r in rows:
            if not isinstance(r, dict) or r.get("id") is None:
                continue
            rid = int(r["id"])
            if r.get("sealed_until_tipoff"):
                sealed.add(rid)
            clean = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                     for k, v in r.items() if k not in JSON_ONLY_FIELDS and IDENT_RE.match(k)}
            clean["id"] = rid
            if rid in by_id:
                if by_id[rid] != clean:
                    differing.append(rid)
                continue  # first copy wins
            by_id[rid] = clean
    if differing:
        notes.append(f"{len(differing)} row id(s) appeared in more than one --ledger-json input with different "
                     f"contents; the first copy was used: {sorted(set(differing))}")
    if sealed:
        notes.append(f"{len(sealed)} row(s) were sealed until tip-off (pick fields withheld); they count as "
                     f"ungraded games until revealed.")

    cols = sorted({k for r in by_id.values() for k in r} - {"id"})
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    # Untyped columns keep each JSON value's own type (int, float, text, null).
    quoted = "".join(', "' + c + '"' for c in cols)
    con.execute("CREATE TABLE predictions_log (id INTEGER PRIMARY KEY" + quoted + ")")
    insert = "INSERT INTO predictions_log (id" + quoted + ") VALUES (" + ",".join("?" * (len(cols) + 1)) + ")"
    for rid in sorted(by_id):
        r = by_id[rid]
        con.execute(insert, [rid] + [r.get(c) for c in cols])
    rows = [dict(r) for r in con.execute("SELECT * FROM predictions_log ORDER BY id")]
    con.close()
    return rows, sealed, notes


def official_id(row: dict, league: League, home_id, away_id, tip: datetime | None) -> tuple[str | None, str]:
    """(game id, how it was found): the id stored when graded from the league's box
    score, else the league game with the same two teams within one day of the
    tip-off's Eastern date."""
    if str(row.get("result_source") or "").strip().lower() == "nba.com":
        gid = norm_game_id(row.get("result_source_ref"))
        if gid is not None:
            return gid, "stored"
    if tip is None:
        return None, "none"
    gid = league.match_game(home_id, away_id, eastern_date(tip))
    return (gid, "matched") if gid is not None else (None, "none")


def side_name(row: dict, league: League, team_id) -> str | None:
    """A league team id written in the ledger row's own spelling."""
    if team_id is None:
        return None
    if league.team_id(row.get("home_team")) == team_id:
        return row.get("home_team")
    if league.team_id(row.get("away_team")) == team_id:
        return row.get("away_team")
    return league.name_by_team.get(team_id, str(team_id))


# ---------------------------------------------------------------------------
# One window
# ---------------------------------------------------------------------------
def compute_window(key: str, season: str, gtype: str, model: str, games: list[dict]) -> dict:
    n = len(games)
    hits = [g["model_hit"] for g in games]
    k = sum(hits)

    window = {
        "key": key, "season": season, "game_type": gtype, "model": model, "n": n,
        "hit": {"k": k, "n": n, "rate": rate(k, n), "wilson95": wilson(k, n)},
        "hit_league_results": None,
    }
    if any(g["conflict"] for g in games):
        kl = sum(g["model_hit_league"] for g in games)
        window["hit_league_results"] = {"k": kl, "n": n, "rate": rate(kl, n)}

    # 4.2 baselines and the x series of section 5
    def baseline(pairs):
        bk = sum(b for _, b in pairs)
        bn = len(pairs)
        return {"k": bk, "n": bn, "rate": rate(bk, bn)}, [float(m - b) for m, b in pairs]

    home_b, home_x = baseline([(g["model_hit"], g["home_hit"]) for g in games])
    rec_b, rec_x = baseline([(g["model_hit"], g["record_hit"]) for g in games if g["record_hit"] is not None])
    if rec_b["n"] == 0:
        rec_b["k"] = None  # no league game log: the better record cannot be computed (null, never 0)
    favl_b, favl_x = baseline([(g["model_hit"], g["fav_logged_hit"]) for g in games if g["fav_logged_hit"] is not None])
    favc_b, favc_x = baseline([(g["model_hit"], g["fav_close_hit"]) for g in games if g["fav_close_hit"] is not None])
    window["baselines"] = {"home": home_b, "better_record": rec_b, "favorite_logged": favl_b, "favorite_close": favc_b}

    # 4.3 probability quality
    def quality(sel, mkt_prop_key, mkt_shin_key):
        if not sel:
            return {"brier": None, "log_loss": None}, {"proportional": {"brier": None, "log_loss": None},
                                                       "shin": {"brier": None, "log_loss": None}}, None
        mb = mean([brier_one(g["p_model"], g["y"]) for g in sel])
        ml = mean([log_loss_one(g["p_model"], g["y"]) for g in sel])
        pb = mean([brier_one(g[mkt_prop_key], g["y"]) for g in sel])
        pl = mean([log_loss_one(g[mkt_prop_key], g["y"]) for g in sel])
        sb = mean([brier_one(g[mkt_shin_key], g["y"]) for g in sel])
        sl = mean([log_loss_one(g[mkt_shin_key], g["y"]) for g in sel])
        skill = (1.0 - mb / pb) if pb else None
        return ({"brier": mb, "log_loss": ml},
                {"proportional": {"brier": pb, "log_loss": pl}, "shin": {"brier": sb, "log_loss": sl}},
                skill)

    priced = [g for g in games if g["p_mkt_prop"] is not None]
    closed = [g for g in games if g["p_close_prop"] is not None]
    m_q, mk_q, skill = quality(priced, "p_mkt_prop", "p_mkt_shin")
    cm_q, cmk_q, cskill = quality(closed, "p_close_prop", "p_close_shin")
    window["probability"] = {
        "n": len(priced),
        "missing_prices": n - len(priced),
        "model": m_q, "market": mk_q, "brier_skill_vs_market": skill,
        "close": {"n": len(closed), "model": cm_q, "market": cmk_q, "brier_skill_vs_market": cskill},
    }

    # 4.4 paired log loss (proportional, logged price)
    d = [log_loss_one(g["p_model"], g["y"]) - log_loss_one(g["p_mkt_prop"], g["y"]) for g in priced]
    window["paired_log_loss"] = {"n": len(d), "mean_d": mean(d), "normal95": normal95(d), "bootstrap95": bootstrap95(d)}

    # 4.5 calibration
    cal = []
    for gi, name in enumerate(CAL_NAMES):
        sel = [g for g in games if g["cal_group"] == gi]
        cn = len(sel)
        ck = sum(g["model_hit"] for g in sel)
        cal.append({
            "group": name, "n": cn,
            "mean_stated": mean([g["confidence"] for g in sel]),
            "hit_rate": rate(ck, cn),
            "wilson95": wilson(ck, cn),
        })
    window["calibration"] = cal

    # 4.7 profit, one unit a pick on our side
    def units_one(won: int, dec: float) -> float:
        return (dec - 1.0) if won else -1.0

    lp = [g for g in games if g["dec_ours"] is not None]
    lu = [units_one(g["model_hit"], g["dec_ours"]) for g in lp]
    cp = [g for g in games if g["dec_ours_close"] is not None]
    cu = [units_one(g["model_hit"], g["dec_ours_close"]) for g in cp]
    window["profit"] = {
        "logged": {
            "n": len(lp),
            "missing_price": n - len(lp),
            "units": sum(lu) if lu else None,
            "per_unit": mean(lu),
            "bootstrap95": bootstrap95(lu),
            "break_even_rate": mean([1.0 / g["dec_ours"] for g in lp]),
            "hit_rate": rate(sum(g["model_hit"] for g in lp), len(lp)),
        },
        "close": {
            "n": len(cp),
            "units": sum(cu) if cu else None,
            "per_unit": mean(cu),
            "bootstrap95": bootstrap95(cu),
        },
    }
    be_x = [g["model_hit"] - 1.0 / g["dec_ours"] for g in lp]

    # 4.6 closing-line value
    def clv_fig(vals):
        vn = len(vals)
        return {
            "n": vn,
            "mean_pts": mean(vals),
            "bootstrap95_pts": bootstrap95(vals),
            "share_positive": rate(sum(1 for v in vals if v > 0), vn),
        }

    cons_w = [g["clv_cons_pts"] for g in games if g["clv_cons_pts"] is not None and g["clv_cons_watched"]]
    same_w = [g["clv_same_pts"] for g in games if g["clv_same_pts"] is not None and g["clv_same_watched"]]
    cons_r = sum(1 for g in games if g["clv_cons_pts"] is not None and not g["clv_cons_watched"])
    same_r = sum(1 for g in games if g["clv_same_pts"] is not None and not g["clv_same_watched"])
    status_counts: dict[str, int] = {}
    for g in games:
        status_counts[g["clv_status"]] = status_counts.get(g["clv_status"], 0) + 1
    window["clv"] = {
        "consensus": clv_fig(cons_w),
        "same_book": clv_fig(same_w),
        "reconstructed": {"consensus_n": cons_r, "same_book_n": same_r},
        "status_counts": dict(sorted(status_counts.items())),
    }

    # 5. sequential comparisons
    mb_x = [brier_one(g["p_mkt_prop"], g["y"]) - brier_one(g["p_model"], g["y"]) for g in priced]
    window["sequential"] = {
        "home": sequential(home_x),
        "better_record": sequential(rec_x),
        "favorite_logged": sequential(favl_x),
        "favorite_close": sequential(favc_x),
        "market_brier": sequential(mb_x),
        "break_even": sequential(be_x),
    }
    return window


# ---------------------------------------------------------------------------
# Commitments (bb-ledger-commit-v1, from the ledger repository's README)
# ---------------------------------------------------------------------------
def canonical_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _six(v):
    if v is None:
        return None
    if isinstance(v, str):
        f = to_float(v)
        return v if f is None else f"{f:.6f}"
    return f"{float(v):.6f}"


def pick_array(rec) -> list:
    """One pick as the README's 12-field array: id an integer, every other value a
    string or null, the three numbers as strings with exactly six decimals."""
    if isinstance(rec, (list, tuple)):
        rec = dict(zip(PICK_FIELDS, rec))
    out = []
    for f in PICK_FIELDS:
        v = rec.get(f)
        if f == "id":
            out.append(int(v))
        elif f in NUMERIC_PICK_FIELDS:
            out.append(_six(v))
        else:
            out.append(None if v is None else (v if isinstance(v, str) else str(v)))
    return out


def picks_sha256(log_date: str, seq: int, nonce: str, picks) -> str:
    rows = sorted((pick_array(p) for p in picks), key=lambda a: a[0])
    return sha256_hex(canonical_json([COMMIT_METHOD, log_date, seq, nonce, rows]))


def chain_sha256(prev: str, log_date: str, seq: int, n_picks: int, committed_at: str, picks_hash: str) -> str:
    return sha256_hex(canonical_json([COMMIT_METHOD, "chain", prev, log_date, seq, n_picks, committed_at, picks_hash]))


def _reveal_list(obj) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("commitments", "revealed", "data", "items"):
            if isinstance(obj.get(k), list):
                return obj[k]
    return []


def _reveal_picks(c: dict):
    for k in ("picks", "rows", "revealed_picks"):
        if isinstance(c.get(k), list):
            return c[k]
    return None


def check_commitments(commit_path: str, reveals_path: str | None, ledger_by_id: dict[int, dict]) -> dict:
    lines = []
    with open(commit_path, encoding="utf-8") as fh:
        for ln, raw in enumerate(fh, 1):
            if raw.strip():
                lines.append((ln, json.loads(raw)))

    chain_breaks, chain_mismatch, other_problems = [], [], []
    prev = ZERO_HASH
    by_day_seq = {}
    for ln, c in lines:
        tag = {"line": ln, "log_date": c.get("log_date"), "seq": c.get("seq")}
        if c.get("method_version") not in (None, COMMIT_METHOD):
            other_problems.append({**tag, "problem": f"method_version {c.get('method_version')!r}"})
        if c.get("prev_chain_sha256") != prev:
            chain_breaks.append({**tag, "expected_prev": prev, "found_prev": c.get("prev_chain_sha256")})
        recomputed = chain_sha256(c.get("prev_chain_sha256"), c.get("log_date"), c.get("seq"),
                                  c.get("n_picks"), c.get("committed_at"), c.get("picks_sha256"))
        if recomputed != c.get("chain_sha256"):
            chain_mismatch.append({**tag, "stored": c.get("chain_sha256"), "recomputed": recomputed})
        ids = c.get("pick_ids")
        if isinstance(ids, list) and c.get("n_picks") is not None and len(ids) != c.get("n_picks"):
            other_problems.append({**tag, "problem": "n_picks differs from len(pick_ids)"})
        ca, ft = parse_utc(c.get("committed_at")), parse_utc(c.get("first_tipoff_utc"))
        if ca is not None and ft is not None and not ca < ft:
            other_problems.append({**tag, "problem": "committed_at is not before first_tipoff_utc"})
        prev = c.get("chain_sha256")
        by_day_seq[(c.get("log_date"), c.get("seq"))] = (ln, c)

    out = {
        "lines": len(lines),
        "chain_links_checked": len(lines),
        "chain_breaks": chain_breaks,
        "chain_hash_mismatches": chain_mismatch,
        "line_problems": other_problems,
        "reveals_checked": 0,
        "picks_hash_matches": 0,
        "picks_hash_mismatches": [],
        "pick_id_mismatches": [],
        "pick_field_mismatches": [],
        "picks_not_in_ledger": [],
        "reveals_without_line": [],
    }
    if not reveals_path:
        return out

    with open(reveals_path, encoding="utf-8") as fh:
        reveals = _reveal_list(json.load(fh))
    for rv in reveals:
        nonce = rv.get("nonce")
        picks = _reveal_picks(rv)
        if blank(nonce) or picks is None:
            continue  # not revealed yet
        key = (rv.get("log_date"), rv.get("seq"))
        if key not in by_day_seq:
            out["reveals_without_line"].append({"log_date": key[0], "seq": key[1]})
            continue
        ln, line = by_day_seq[key]
        out["reveals_checked"] += 1
        arrays = sorted((pick_array(p) for p in picks), key=lambda a: a[0])
        recomputed = picks_sha256(line.get("log_date"), line.get("seq"), nonce, picks)
        tag = {"line": ln, "log_date": key[0], "seq": key[1]}
        if recomputed == line.get("picks_sha256"):
            out["picks_hash_matches"] += 1
        else:
            out["picks_hash_mismatches"].append({**tag, "stored": line.get("picks_sha256"), "recomputed": recomputed})
        ids = [a[0] for a in arrays]
        if isinstance(line.get("pick_ids"), list) and sorted(int(i) for i in line["pick_ids"]) != ids:
            out["pick_id_mismatches"].append({**tag, "line_ids": line.get("pick_ids"), "revealed_ids": ids})
        for a in arrays:
            led = ledger_by_id.get(a[0])
            if led is None:
                out["picks_not_in_ledger"].append(a[0])
                continue
            want = pick_array(led)
            for f, got, exp in zip(PICK_FIELDS, a, want):
                if got != exp:
                    out["pick_field_mismatches"].append({"id": a[0], "field": f, "revealed": got, "ledger": exp})
    return out


# ---------------------------------------------------------------------------
# The whole record
# ---------------------------------------------------------------------------
def reconcile(ledger_path: str | None, league_path: str | None, commitments: str | None = None,
              reveals: str | None = None, ledger_json: list[str] | None = None) -> dict:
    notes: list[str] = []
    league = League(league_path)
    if ledger_json:
        rows, sealed_ids, json_notes = load_ledger_json(ledger_json)
        notes += json_notes
    else:
        rows, sealed_ids = load_ledger(ledger_path), set()
    if not league.available:
        notes.append("No --league given: the better-record baseline could not be computed (null), games graded "
                     "from ESPN's final have no official game id (counted as 'unconfirmed', season from the "
                     "tip-off's Eastern date), conflicts carry no league winner, and the integrity checks against "
                     "the league and ESPN were not run.")

    integrity = {
        "logged_at_not_before_tip_ids": [],
        "unparseable_time_ids": [],
        "id_gaps": [],
        "winner_differs_from_league": [],
        "winner_differs_from_espn": [],
        "stored_game_id_not_in_league_ids": [],
        "unmapped_team_names": [],
        "winner_not_home_or_away_ids": [],
        "graded_without_pick_ids": [],
        "checks_run": {},
    }

    # --- exclusions -------------------------------------------------------
    excluded = {"other_sport": 0, "other_book": 0, "simulated": 0, "duplicate_later_log": 0}
    candidates = []
    unmapped = set()
    for r in rows:
        if r.get("sport") != COUNTED_SPORT:
            excluded["other_sport"] += 1
            continue
        if r.get("sportsbook") != COUNTED_BOOK:
            excluded["other_book"] += 1
            continue
        if r.get("model") == SIMULATED_MODEL:
            excluded["simulated"] += 1
            continue
        candidates.append(r)

    def enrich(r: dict) -> dict:
        tip = parse_utc(r.get("game_start_time_utc"))
        h, a = league.team_id(r.get("home_team")), league.team_id(r.get("away_team"))
        for nm, tid in ((r.get("home_team"), h), (r.get("away_team"), a)):
            if tid is None and not blank(nm):
                unmapped.add(str(nm))
        gid, how = official_id(r, league, h, a, tip)
        return {"row": r, "tip": tip, "home_id": h, "away_id": a, "gid": gid, "gid_how": how,
                "logged": parse_utc(r.get("logged_at"))}

    enriched = [enrich(r) for r in candidates]

    # --- one pick per game ------------------------------------------------
    groups: dict = {}
    for e in enriched:
        key = ("gid", e["gid"]) if e["gid"] is not None else ("pair", e["row"].get("game_key"), e["row"].get("game_start_time_utc"))
        groups.setdefault(key, []).append(e)

    def first_key(e):
        lg = e["logged"]
        # earliest logged_at, then the lower row id (unparseable times sort last)
        return (0, lg, e["row"]["id"]) if lg is not None else (1, datetime.max.replace(tzinfo=timezone.utc), e["row"]["id"])

    counted = []
    for members in groups.values():
        members.sort(key=first_key)
        counted.append(members[0])
        excluded["duplicate_later_log"] += len(members) - 1
    counted.sort(key=lambda e: e["row"]["id"])

    # --- grading, ids, per-game values -------------------------------------
    graded_games, ungraded_ids, unconfirmed_ids, conflicts, below50 = [], [], [], [], []
    for e in counted:
        r = e["row"]
        rid = r["id"]
        if blank(r.get("actual_winner")):
            ungraded_ids.append(rid)
            continue
        if blank(r.get("predicted_winner")) or to_float(r.get("winner_confidence")) is None or e["tip"] is None:
            integrity["graded_without_pick_ids"].append(rid)
            continue

        gid = e["gid"]
        league_game = league.games.get(gid) if gid else None
        if gid is None:
            unconfirmed_ids.append(rid)
            ed = eastern_date(e["tip"])
            season, gtype, rec_day = season_from_date(ed), UNCONFIRMED, ed
        else:
            season, gtype = season_from_id(gid), type_from_id(gid)
            rec_day = league_game["date"] if league_game and league_game["date"] else eastern_date(e["tip"])
            if league_game is None and league.available:
                integrity["stored_game_id_not_in_league_ids"].append(rid)

        home, away = r.get("home_team"), r.get("away_team")
        winner = r.get("actual_winner")
        pick = r.get("predicted_winner")
        conf = to_float(r.get("winner_confidence"))
        if winner != home and winner != away:
            integrity["winner_not_home_or_away_ids"].append(rid)
        y = 1 if winner == home else 0
        model_hit = 1 if pick == winner else 0

        # league result (for conflicts)
        league_winner = None
        if league_game is not None and league_game["winner"] is not None:
            league_winner = side_name(r, league, league_game["winner"])
        is_conflict = not blank(r.get("result_conflict"))
        if is_conflict:
            conflicts.append({"id": rid, "ledger_winner": winner, "league_winner": league_winner})
        result_for_league = league_winner if (is_conflict and league_winner is not None) else winner

        if conf < 50:
            below50.append(rid)

        # better record
        yy = season_yy_from_label(season)
        rec_pick = home
        if e["home_id"] is not None and e["away_id"] is not None:
            hw, hg = league.record_before(yy, e["home_id"], rec_day)
            aw, ag = league.record_before(yy, e["away_id"], rec_day)
            if hg > 0 and ag > 0 and aw * hg > hw * ag:  # away's win% strictly higher
                rec_pick = away

        # market at the logged price
        pi_h, pi_a = implied(r.get("home_ml")), implied(r.get("away_ml"))
        if pi_h is not None and pi_a is not None:
            fav = home if pi_h >= pi_a else away
            fav_logged_hit = 1 if fav == winner else 0
            p_mkt_prop, p_mkt_shin = proportional(pi_h, pi_a), shin_home(pi_h, pi_a)
        else:
            fav_logged_hit = p_mkt_prop = p_mkt_shin = None

        # market at the close (same book, clv_status = priced, both prices present)
        ci_h, ci_a = implied(r.get("closing_home_ml")), implied(r.get("closing_away_ml"))
        if r.get("clv_status") == "priced" and ci_h is not None and ci_a is not None:
            cfav = home if ci_h >= ci_a else away
            fav_close_hit = 1 if cfav == winner else 0
            p_close_prop, p_close_shin = proportional(ci_h, ci_a), shin_home(ci_h, ci_a)
        else:
            fav_close_hit = p_close_prop = p_close_shin = None

        # profit (4.7): our side's price is the home price if we picked home, else the away price
        ours_home = pick == home
        dec_ours = american_to_decimal(r.get("home_ml") if ours_home else r.get("away_ml"))
        dec_ours_close = (american_to_decimal(r.get("closing_home_ml") if ours_home else r.get("closing_away_ml"))
                          if r.get("clv_status") == "priced" else None)

        # closing-line value
        same_pts = to_float(r.get("clv_prob")) if r.get("clv_status") == "priced" else None
        cons_pts = to_float(r.get("clv_consensus_prob")) if r.get("clv_consensus_status") == "priced" else None
        same_prov = r.get("closing_provenance")
        cons_prov = r.get("consensus_provenance")

        graded_games.append({
            "id": rid, "tip": e["tip"], "season": season, "type": gtype,
            "model": r.get("model") if not blank(r.get("model")) else "unknown",
            "y": y, "confidence": conf,
            "p_model": conf / 100.0 if pick == home else 1.0 - conf / 100.0,
            "model_hit": model_hit,
            "model_hit_league": 1 if pick == result_for_league else 0,
            "conflict": is_conflict,
            "home_hit": y,
            "record_hit": (1 if rec_pick == winner else 0) if league.available else None,
            "fav_logged_hit": fav_logged_hit, "p_mkt_prop": p_mkt_prop, "p_mkt_shin": p_mkt_shin,
            "fav_close_hit": fav_close_hit, "p_close_prop": p_close_prop, "p_close_shin": p_close_shin,
            "dec_ours": dec_ours, "dec_ours_close": dec_ours_close,
            "clv_same_pts": None if same_pts is None else 100.0 * same_pts,
            "clv_same_watched": blank(same_prov) or same_prov == "observed",
            "clv_cons_pts": None if cons_pts is None else 100.0 * cons_pts,
            "clv_cons_watched": cons_prov == "observed",
            "clv_status": r.get("clv_status") if r.get("clv_status") is not None else "unsettled",
            "cal_group": cal_group(conf),
        })

    # --- windows ------------------------------------------------------------
    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for g in graded_games:
        for s in (g["season"], "all-time"):
            for t in ("all", g["type"]):
                buckets.setdefault((s, t, g["model"]), []).append(g)
    windows = []
    for (s, t, m), games in buckets.items():
        games.sort(key=lambda g: (g["tip"], g["id"]))
        if games:
            windows.append(compute_window(f"{s}|{t}|{m}", s, t, m, games))
    windows.sort(key=lambda w: w["key"])

    # --- integrity (separate from the plan's figures) ------------------------
    ids = [r["id"] for r in rows]
    for r in rows:
        la, st = parse_utc(r.get("logged_at")), parse_utc(r.get("game_start_time_utc"))
        if la is None or st is None:
            if r["id"] not in sealed_ids:
                integrity["unparseable_time_ids"].append(r["id"])
        elif not la < st:
            integrity["logged_at_not_before_tip_ids"].append(r["id"])
    if ids:
        present = set(ids)
        integrity["id_gaps"] = [i for i in range(1, max(ids) + 1) if i not in present]

    league_checked = espn_checked = 0
    for r in rows:
        if blank(r.get("actual_winner")):
            continue
        tip = parse_utc(r.get("game_start_time_utc"))
        h, a = league.team_id(r.get("home_team")), league.team_id(r.get("away_team"))
        gid, _ = official_id(r, league, h, a, tip)
        lg = league.games.get(gid) if gid else None
        flagged = not blank(r.get("result_conflict"))
        if lg is not None and lg["winner"] is not None:
            league_checked += 1
            lw = side_name(r, league, lg["winner"])
            if lw != r.get("actual_winner"):
                integrity["winner_differs_from_league"].append(
                    {"id": r["id"], "game_id": gid, "ledger_winner": r.get("actual_winner"),
                     "league_winner": lw, "ledger_flags_conflict": flagged})
        ref = r.get("result_source_ref") if str(r.get("result_source") or "").strip().lower() == "espn" else None
        eid = league.match_espn(ref, h, a, eastern_date(tip) if tip else None)
        if eid is not None:
            ev = league.espn_by_id[eid]
            hp, ap = to_float(ev["home_pts"]), to_float(ev["away_pts"])
            if hp is not None and ap is not None and hp != ap and ev["home"] is not None:
                espn_checked += 1
                wid = int(ev["home"]) if hp > ap else int(ev["away"])
                ew = side_name(r, league, wid)
                if ew != r.get("actual_winner"):
                    integrity["winner_differs_from_espn"].append(
                        {"id": r["id"], "espn_event_id": eid, "ledger_winner": r.get("actual_winner"),
                         "espn_winner": ew, "ledger_flags_conflict": flagged})
    integrity["unmapped_team_names"] = sorted(unmapped) if league.available else []
    if ledger_json and integrity["id_gaps"]:
        notes.append("id_gaps covers only the rows the --ledger-json inputs held; ids of seasons not fetched "
                     "show up as gaps.")
    integrity["checks_run"] = {
        "league_given": league.available,
        "rows_checked_logged_before_tip": len(rows),
        "id_range_checked": [1, max(ids)] if ids else None,
        "graded_rows": sum(1 for r in rows if not blank(r.get("actual_winner"))),
        "graded_rows_checked_against_league": league_checked,
        "espn_table_present": league.has_espn,
        "graded_rows_checked_against_espn": espn_checked,
    }
    if commitments:
        integrity["commitments"] = check_commitments(commitments, reveals, {r["id"]: r for r in rows})

    return {
        "method": METHOD,
        "counted": {
            "rows_total": len(rows),
            "excluded": excluded,
            "games": len(counted),
            "graded": len(counted) - len(ungraded_ids),
            "ungraded_ids": sorted(ungraded_ids),
            "unconfirmed_ids": sorted(unconfirmed_ids),
            "conflicts": sorted(conflicts, key=lambda c: c["id"]),
            "below_50_confidence_ids": sorted(below50),
        },
        "windows": windows,
        "integrity": integrity,
        "notes": notes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Independently recompute Betting Buddy's live NBA record (bb-nba-live-eval-v1) from the ledger\n"
            "and the league's game log. Standard library only; on Windows the America/New_York time zone\n"
            "may need `pip install tzdata`.\n\n"
            "The plan:        github.com/altink14/bettingbuddy-ledger nba/EVALUATION_PLAN_v1.md\n"
            "                 (backend: docs/ledger/EVALUATION_PLAN_v1.md)\n"
            "The output spec: nba/audit/OUTPUT.md (backend: docs/ledger/live_eval_output.md)\n"
            "This script:     nba/audit/reconcile_live_record.py (backend: audit/reconcile_live_record.py)"),
        epilog=(
            "Public data only, one --ledger-json per season:\n"
            "  python reconcile_live_record.py --ledger-json \\\n"
            "    \"https://nba-machine-learning-sports-betting-production-499f.up.railway.app"
            "/api/prediction-log?season=2026-27\""))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ledger", help="OddsData.sqlite holding predictions_log (opened read-only)")
    src.add_argument("--ledger-json", action="append", metavar="PATH_OR_URL",
                     help="the public GET /api/prediction-log?season=YYYY-YY JSON, as a URL (fetched, 60 s timeout) "
                          "or a saved file; repeat once per season (rows are de-duplicated by id)")
    ap.add_argument("--league",
                    help="TeamData.sqlite holding team_metadata, game_results and optionally espn_box_scores "
                         "(read-only). Optional: without it the better-record baseline and the game ids of "
                         "ESPN-graded games cannot be computed, and the output's notes say so")
    ap.add_argument("--out", help="write the JSON here (default: stdout)")
    ap.add_argument("--commitments", help="a commitments.jsonl from the public ledger repository")
    ap.add_argument("--reveals", help="JSON of revealed commitments (nonce + picks), as GET /api/ledger/commitments returns")
    args = ap.parse_args(argv)
    if args.reveals and not args.commitments:
        ap.error("--reveals needs --commitments")

    result = reconcile(args.ledger, args.league, args.commitments, args.reveals, args.ledger_json)
    text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
