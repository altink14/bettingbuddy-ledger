# How Betting Buddy's live NBA record is judged (bb-nba-live-eval-v1)

> **Published 2026-10-15** to github.com/altink14/bettingbuddy-ledger as
> `nba/EVALUATION_PLAN_v1.md`, five days before the first game of the 2026-27
> season (2026-10-20). GitHub's record of that push is its date.

This file fixes, before a single live game is played, how the record of our
NBA moneyline picks will be measured and what the site may say about it.
Whoever chooses the window, the yardstick or the test after seeing results
can make any record look good. Writing them down first, somewhere we cannot
quietly edit, is what stops us from doing that, including by accident.

**Rules for this file.** It is never edited after publication. A change is a
new file (`EVALUATION_PLAN_v2.md`, and so on) that says what changed and why,
and it applies only to games that tip off after GitHub received it. Every
figure the site shows about the live record is computed by the method below,
and the method's name, `bb-nba-live-eval-v1`, is printed beside it.

## 1. What a pick is, and which picks count

- A pick is one row of our prediction ledger (`predictions_log`): the
  model's predicted winner, its stated confidence in that winner (a percent),
  and the moneyline prices it was logged at, written before tip-off. The
  database refuses a row logged at or after tip-off, refuses any later change
  to the pick, and refuses to regrade a graded pick.
- **Counted:** NBA rows (`sport` = `NBA`) logged for the ledger's one book,
  FanDuel (`sportsbook` = `fanduel`), by the model (rows tagged
  `implied_probability_sim`, simulated market fallbacks, are never counted).
- **One pick per game.** If a game was logged more than once (on two
  different days), only the earliest-logged row counts.
- **Graded picks only.** A pick enters any figure once its final score is
  recorded. Postponed games that never finish are never graded and never
  counted; they are listed.
- **The result** is the one stored in the ledger. If our ledger was graded
  from ESPN's final score and the league's own box score later disagreed,
  the ledger marks a conflict (a grade is never changed). Conflicts are
  listed by game, and while any exist the hit rate is also given with the
  league's result.

## 2. Seasons and game types come from the league's game id

Each graded pick is tied to its official NBA game id: the id stored when it
was graded from the league's box score, or else the game in the league's
game log between the same two teams (either way round: a neutral-site game
may be filed with the other team at home) within one day of the logged
tip-off's US Eastern date. The id decides both:

- **Season:** characters 4-5 of the id (`0022600001` is the 2026-27 season).
- **Game type:** characters 1-3: `002` regular season, `005` play-in, `004`
  playoffs, `006` NBA Cup final. Preseason (`001`) is never logged.

A pick whose official game has not yet appeared in the league's data is
placed by the Eastern date of its tip-off (a season runs from 1 October to
30 September) and its type is "unconfirmed". The number of unconfirmed
picks is always shown.

## 3. Windows: fixed, never chosen after the fact

1. **Each season to date**, all game types together, and each type on its own.
2. **All-time**, from the first live pick.
3. Both, **separately for each model version** (the ledger's `model` tag).
   Picks from different model versions are never pooled into one figure.

The site's headline is the current season, all game types, the serving model
(the model version of the most recently logged pick).
No other window (last 30 days, last month, "since the fix", hot streaks) is
ever presented as the record.

## 4. The measures

For each pick, `y` = 1 if the home team won, else 0. The model's probability
for the home team is its confidence divided by 100 if it picked the home team,
otherwise 1 minus that. Probabilities used in logarithms are clipped to
[0.000001, 0.999999].

**4.1 Hit rate.** Picks whose predicted winner won, out of graded picks, with
a 95% Wilson score interval.

**4.2 Baselines, on exactly the same games** (each scored as a pick, hits out
of the same graded games):

- **Always the home team.**
- **The better record:** the team with the higher win percentage in that
  season's regular-season games (game ids starting `002`) played before the
  game's date, from the league's game log; the home team when equal or when
  either team has not played yet.
- **The market's favorite at the logged price:** the side with the shorter
  moneyline in the ledger row (the home team if the prices are equal).
- **The market's favorite at the close:** the same, using the ledger's
  closing prices on the games where the closing-line method (`bb-nba-clv-v1`)
  found a valid close at the same book (`clv_status` = `priced`). Its count of
  games is shown.

We state plainly: the market's favorite is the hardest of these. The sealed
evaluation that produced our 67.2% figure never measured it.

**4.3 Probability quality.** On the same games, for the model and for the
market:

- the market's probability for the home team is the price's implied
  probability with the bookmaker's margin removed, two ways:
  *proportional* (each side's implied probability divided by their sum) and
  *Shin's method* (two outcomes; Shin's z found by bisection so the two
  probabilities sum to 1). When a pair of prices carries no margin, both
  methods are the plain normalization. A game whose logged prices are missing
  is left out of 4.3 and 4.4 and its count is shown;
- **Brier score**: the mean of (p − y)²;
- **log loss**: the mean of −[y ln p + (1 − y) ln(1 − p)];
- **Brier skill score** against the market (proportional, logged price):
  1 − model Brier / market Brier. Above 0 means the model's probabilities
  were closer to what happened than the market's.

The same figures are computed against the closing price, on games with a close.

**4.4 Model against market, paired.** For each game,
d = model log loss − market log loss (proportional, logged price). We report
the mean of d with two 95% intervals: the normal approximation
(mean ± z × sd / √n with z from the appendix, the Diebold-Mariano form for
one-step forecasts) and
a paired bootstrap (10,000 resamples of games, random seed 20261020,
2.5th and 97.5th percentiles). A negative mean favors the model.

**4.5 Calibration.** Picks are grouped by the model's stated confidence in its
pick: 50-55, 55-60, 60-65, 65-70, 70-75, 75-80, 80-85 and 85-100 percent (each
group includes its lower edge). For each group: the number of picks, the
average stated confidence, the hit rate and its 95% Wilson interval. The site
describes a group in words only when it holds at least 30 picks.

**4.6 Closing-line value.** As defined by `bb-nba-clv-v1` (documented in the
backend's `src/Utils/nba_clv.py`), two figures: against the consensus close
(the median no-margin probability over every book with a close) and against
the same book's close. For each, on picks priced from closes we watched
(reconstructed closes are counted apart and never pooled): the mean CLV in
probability points (the ledger's `clv_consensus_prob` and `clv_prob`: the
no-margin probability of our side at the close minus at the price we
logged) with a bootstrap 95% interval (10,000 resamples, seed 20261020), the
share of picks that beat the close, and the count of picks in each
settlement status. CLV says whether the price we logged was better than
the market's last price; it is evidence about an edge, not profit.

**4.7 Profit, one unit a pick.** One unit staked on every counted, graded
pick, on the team we picked, at the price it was logged at (the home price if
we picked the home team, else the away price): a win returns (decimal price −
1) units, a loss −1. A pick whose price for our side is missing is left out
and counted. We report:

- the number of picks, the total units won, and the **return per unit
  staked** (total units ÷ picks) with a bootstrap 95% interval (10,000
  resamples, seed 20261020);
- the **break-even rate**: the average of 1 / decimal price over those picks,
  the share of them we needed to win to break even at those prices (it
  includes the bookmaker's margin), beside the share we did win;
- the same return at the **closing price** for our side, on the picks with a
  valid same-book close (`clv_status` = `priced`), as a reference for "if the
  pick had been bet at the last price".

This is a measurement of what happened, not a forecast, and it is never shown
without its interval. Past results do not predict future returns. The figure
appears only on the record itself, never in advertising.

## 5. When the site may say "ahead" or "behind"

The record is updated every day and read every day. A fixed-sample interval
looked at every day will, sooner or later, cross any line by chance alone. So
every sentence comparing the model with a baseline uses an interval that
stays valid however often it is checked: a **two-sided 95% normal-mixture
confidence sequence** (Robbins; Howard, Ramdas, McAuliffe and Sekhon 2021).

For a comparison, each game gives a number x between −1 and 1:

- against a baseline pick: x = (model hit) − (baseline hit), which is 1, 0 or −1;
- against the market's probabilities: x = market Brier − model Brier for the
  game (positive favors the model);
- against the price (4.7): x = (model hit) − 1 / (decimal price taken), the
  win beyond what the price required. "Ahead" here means the picks won more
  often than the prices we took needed them to; the return per unit has the
  same sign whenever the prices are similar, and its own interval is always
  shown beside it.

After n games with mean x̄, the sequence is

    x̄ ± sqrt( (n·σ² + ρ) · ln( (n·σ² + ρ) / (ρ·α²) ) ) / n

with α = 0.05, σ² = 1 (Hoeffding's bound for values in [−1, 1]) and
ρ = 1000 / (2·ln(1/α) + ln(1 + 2·ln(1/α))) ≈ 126.0, which makes the sequence
tightest near 1,000 games. Any ρ > 0 gives a valid sequence; this one is
fixed here so it cannot be tuned later.

- **"Ahead of X"** only while the whole sequence is above 0.
- **"Behind X"** whenever the whole sequence is below 0, said as plainly as
  "ahead" would be.
- Otherwise, including before there is any game to compare: **"not yet
  distinguishable from X"**, with the number of games.

"The whole sequence" means the interval as it stands after the latest game.

This is deliberately conservative: it treats every game as able to swing the
comparison fully. Separately, the single-season hit rate is described with its
Wilson interval, labeled as a description of that season, not a claim of skill.

## 6. What anyone can check without trusting us

1. **The picks existed before the games.** Each day's fingerprint is pushed to
   this repository before its first tip-off (see README.md), and GitHub
   records when each push arrived. After the games, the picks and the secret
   are revealed and anyone can recompute the hashes.
2. **The results are the league's.** Every graded game names its source (the
   league's box score, or ESPN's final while the league's was late, later
   confirmed against the league's).
3. **The market is independent of us.** The baselines and closing-line value
   use bookmakers' prices recorded before each game.
4. **The arithmetic.** A reconciliation script, written separately from the
   site's code and published with this plan (`nba/audit/` in this
   repository, standard-library Python), recomputes every figure from the
   revealed picks and the league's results. It runs on the picks the site's
   public API serves after each game.
5. **This file's date.** GitHub's record shows it arrived before the first
   game it governs.

## 7. What this plan does not claim

It does not promise any result. Our sealed pre-season evaluation found 67.2%
moneyline accuracy on 2,597 games against 55.1% for always picking the home
team; it did not compare the model with the market's favorite, and it did not
measure whether the picks make money: from opening night of 2026-27, 4.7
measures that in public, whatever it shows. This file only fixes how the live
record will be judged, so the judgment is the same whatever the games bring.

## Appendix: the exact arithmetic

So that anyone can reproduce every figure to the last decimal:

- **Prices.** An American price x ≥ 100 is decimal 1 + x/100; x ≤ −100 is
  1 + 100/(−x); anything else is missing. Implied probability = 1 / decimal.
  The favorite is the side with the higher implied probability; equal → home.
- **Shin, two outcomes.** With implied probabilities π_h, π_a and their sum β:
  if β ≤ 1, use the proportional method. Otherwise
  p_i(z) = (sqrt(z² + 4(1 − z)·π_i²/β) − z) / (2(1 − z)), and z is found by
  bisection on [0, 1 − 10⁻¹²] for p_h(z) + p_a(z) = 1, stopping after 200
  halvings or when the bracket is narrower than 10⁻¹⁵.
- **Eastern dates** use the America/New_York time zone database.
- **Matching a game.** Team names map to the league's team ids by full name,
  ignoring case; "LA Clippers" is the Los Angeles Clippers. In the game log
  the home team's row reads "vs." in its matchup. Among candidate games within
  one day of the tip-off's Eastern date, the nearest date wins, then the
  earlier.
- **One per game** is keyed on the official game id, or on the pair (game_key,
  game_start_time_utc) when there is none; the earliest `logged_at` wins,
  then the lower row id.
- **Windows** take games in order of tip-off, then row id. Windows are emitted
  for every season present and for all-time, each for all game types and for
  each type present, each for every model version on its own.
- **Wilson** uses z = 1.959963984540054. **The normal interval** in 4.4 uses
  the same z and the sample standard deviation (divisor n − 1); with fewer
  than two games it is not given.
- **Bootstrap.** For each figure separately, a fresh Python
  `random.Random(20261020)`; B = 10,000 resamples, each the mean of
  n values drawn as `[rng.randrange(n) for _ in range(n)]` over the window's
  ordered list; the means sorted ascending; the interval is
  [means[250], means[9749]]. With no values it is not given.
- **Anomalies:** a graded pick whose stated confidence is below 50 is listed
  (`below_50_confidence_ids`) and left out of the calibration groups.
- **Calibration** puts a confidence c in the group whose lower edge is the
  largest of 50, 55, …, 85 not above c; c below 50 is listed as an anomaly.
- **Logarithms** are natural; probabilities are clipped to [10⁻⁶, 1 − 10⁻⁶]
  before any logarithm.
- **Profit** (4.7): the decimal price follows the price rule above; the
  return per unit is the mean of the per-pick units in window order, and its
  bootstrap uses the same procedure as every other figure; the break-even
  rate is the mean of 1 / decimal over the same picks.
- **Closing-line value** in points is 100 × the stored fraction. A same-book
  close counts as watched when its provenance is empty or "observed"; a
  consensus close when its provenance is "observed".
