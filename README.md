# Betting Buddy: NBA pick commitments

Betting Buddy logs every NBA pick its model makes **before tip-off** and grades
it in public. This repository is the part we cannot quietly change: each day,
a fingerprint (SHA-256 hash) of that day's picks is pushed here **before the
games start**.

The fingerprint reveals nothing about the picks. After the last game it covers
has tipped off, Betting Buddy reveals the picks and the secret that went into
the hash, and anyone can recompute it. If a pick had been changed, added or
removed after the fact, the hash would no longer match the line published here.

## What is in `nba/commitments.jsonl`

One JSON line per commitment, in order. The file only ever grows.

| Field | Meaning |
|---|---|
| `log_date` | The day the picks were logged (US Eastern). |
| `seq` | 1 for the day's first commitment; 2, 3, … for games added later that day. |
| `n_picks`, `pick_ids` | How many picks it covers, and their ids in the public record. |
| `committed_at` | When the commitment was made (UTC, the logging machine's clock). |
| `first_tipoff_utc` | The earliest tip-off among the games it covers. |
| `picks_sha256` | SHA-256 of the day's picks plus a random 64-hex-character nonce. |
| `prev_chain_sha256` | The previous commitment's `chain_sha256` (64 zeros for the first). |
| `chain_sha256` | SHA-256 linking this commitment to the previous one. |
| `method_version` | The hashing recipe: `bb-ledger-commit-v1`. |

Because every `chain_sha256` covers the previous one, editing any past day
would change every line after it, and the copies that people have already
pulled from this repository would expose that.

## How to verify a day yourself

1. Open the **Track Record** at https://bettingbuddy.net/track-record. It is
   public, with no account needed. Its **Verify it yourself** panel recomputes
   every revealed commitment in your browser and compares it with these hashes.
2. Or do it by hand. Once a day's games have tipped off, Betting Buddy's public
   API reveals each commitment's `nonce` and its picks:

       https://nba-machine-learning-sports-betting-production-499f.up.railway.app/api/ledger/commitments

   (`?days=N` sets how far back it reaches; the default is 120 days.) The
   recipe, `bb-ledger-commit-v1`:

   - Each pick becomes a JSON array of exactly these fields, rows in ascending
     `id` order:
     `[id, log_date, sport, sportsbook, game_key, game_start_time_utc, logged_at, model, predicted_winner, winner_confidence, home_ml, away_ml]`.
     `id` is an integer; every other value is a string or `null`. The three
     numbers (`winner_confidence` in percent, `home_ml` and `away_ml` in
     American odds) are written as strings with exactly six decimals, e.g.
     `"67.230000"` and `"-150.000000"`.
   - The preimage is the UTF-8 JSON, with no whitespace, of
     `["bb-ledger-commit-v1", log_date, seq, nonce, [row, row, ...]]`.
     `picks_sha256` = SHA-256 of that preimage, lowercase hex.
   - `chain_sha256` = SHA-256 of the JSON, again with no whitespace, of
     `["bb-ledger-commit-v1", "chain", prev_chain_sha256, log_date, seq, n_picks, committed_at, picks_sha256]`.

   A browser's `JSON.stringify` produces byte-identical arrays, so the check
   fits in a few lines of JavaScript, or in Python with
   `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`.

## What this does and does not prove

- **It proves** that a published day's picks were not edited, added or
  removed after the line was pushed here.
- **The time:** a commit's own date is whatever the publishing machine's clock
  says. The independent evidence is GitHub's record of when each push arrived
  (this repository's activity), and the copies others pulled at the time.
- **It does not prove** the model is any good. The track record is where the
  picks are graded, wins and losses alike, against the baseline they have to
  beat.

Published automatically by Betting Buddy's daily and hourly jobs
(`publish_commitments.py`), which refuse to write anything if this file and
Betting Buddy's own record ever disagree.

## How the record is judged

`nba/EVALUATION_PLAN_v1.md` fixes, before the first game of the 2026-27
season, how the live record is measured: which picks count, the windows, the
yardsticks the model is compared with (including the betting market's own
favorite), probability scores, calibration, closing-line value, profit at one
unit a pick, and the rule for when Betting Buddy may say the model is "ahead"
or "behind". Like the commitments, it is never edited; a change would be a new,
dated file that applies only to later games.

`nba/audit/reconcile_live_record.py` recomputes every figure from the plan
alone. It was written separately from Betting Buddy's own code, and the two
must agree on every number. It needs only Python's standard library (on
Windows, `pip install tzdata` for the time zone database). It runs on the picks
Betting Buddy's public API serves once each game has started:

    python nba/audit/reconcile_live_record.py --ledger-json "https://nba-machine-learning-sports-betting-production-499f.up.railway.app/api/prediction-log?season=2026-27"

Without the league's game log it says which figures it could not compute (the
better-record yardstick, and the game ids of games graded from ESPN's score).
`nba/audit/OUTPUT.md` describes what it prints.
