# bb-nba-live-eval-v1: the output both implementations emit

Two programs compute the live record from `nba/EVALUATION_PLAN_v1.md`: Betting
Buddy's own scorer (served at `/api/track-record/evaluation` on its public API)
and the independent checker in this folder (`reconcile_live_record.py`),
written separately, from the plan alone. Both print this JSON, so their outputs can be
compared field by field. Floats are full precision (no rounding); a figure that
cannot be computed is `null`, never 0.

```jsonc
{
  "method": "bb-nba-live-eval-v1",
  "counted": {
    "rows_total": 0,                 // every row in predictions_log
    "excluded": {"other_sport": 0, "other_book": 0, "simulated": 0, "duplicate_later_log": 0},
    "games": 0,                      // after exclusions and one-per-game
    "graded": 0,
    "ungraded_ids": [12, 40],        // row ids, ascending (postponed or pending)
    "unconfirmed_ids": [],           // graded rows with no official game id yet
    "conflicts": [                   // ascending row id
      {"id": 0, "ledger_winner": "", "league_winner": ""}   // league_winner null if the league has no result
    ],
    "below_50_confidence_ids": []
  },
  "windows": [                       // sorted by key
    {
      "key": "2026-27|all|candidate_2026-08",  // season or "all-time" | game type | model
      "season": "2026-27",           // or "all-time"
      "game_type": "all",            // all | regular_season | play_in | playoffs | cup_final | unconfirmed
      "model": "candidate_2026-08",
      "n": 0,                        // graded games in the window
      "hit": {"k": 0, "n": 0, "rate": null, "wilson95": [null, null]},
      "hit_league_results": null,    // {"k","n","rate"} only when the window holds a conflict
      "baselines": {                 // each {"k","n","rate"}; rate null when n = 0
        "home": {}, "better_record": {}, "favorite_logged": {}, "favorite_close": {}
      },
      "probability": {
        "n": 0,                      // games with both logged prices
        "missing_prices": 0,
        "model": {"brier": null, "log_loss": null},
        "market": {"proportional": {"brier": null, "log_loss": null},
                   "shin": {"brier": null, "log_loss": null}},
        "brier_skill_vs_market": null,   // 1 - model/market(proportional)
        "close": {
          "n": 0,                    // games with a priced same-book close and both closing prices
          "model": {"brier": null, "log_loss": null},
          "market": {"proportional": {"brier": null, "log_loss": null},
                     "shin": {"brier": null, "log_loss": null}},
          "brier_skill_vs_market": null
        }
      },
      "paired_log_loss": {"n": 0, "mean_d": null, "normal95": [null, null], "bootstrap95": [null, null]},
      "calibration": [               // the eight groups, always all eight, in order
        {"group": "50-55", "n": 0, "mean_stated": null, "hit_rate": null, "wilson95": [null, null]}
        // ... "55-60", "60-65", "65-70", "70-75", "75-80", "80-85", "85-100"
      ],
      "profit": {                   // 4.7: one unit a pick on our side
        "logged": {"n": 0, "missing_price": 0, "units": null, "per_unit": null,
                   "bootstrap95": [null, null], "break_even_rate": null, "hit_rate": null},
        "close": {"n": 0, "units": null, "per_unit": null, "bootstrap95": [null, null]}
      },
      "clv": {
        "consensus": {"n": 0, "mean_pts": null, "bootstrap95_pts": [null, null], "share_positive": null},
        "same_book": {"n": 0, "mean_pts": null, "bootstrap95_pts": [null, null], "share_positive": null},
        "reconstructed": {"consensus_n": 0, "same_book_n": 0},
        "status_counts": {"priced": 0}   // same-book clv_status of graded games; NULL counted as "unsettled"
      },
      "sequential": {                // each {"n","mean","cs95":[lo,hi],"verdict"}; verdict ahead | behind | not_yet
        "home": {}, "better_record": {}, "favorite_logged": {}, "favorite_close": {}, "market_brier": {},
        "break_even": {}             // x = hit - 1/decimal price taken, over profit.logged's picks
      }
    }
  ]
}
```

Notes that pin down what the plan leaves implicit:

- A comparison with no games has verdict `not_yet` and null mean and interval.
- Without the league's game log, `baselines.better_record` is
  `{"k": null, "n": 0, "rate": null}`: not computable, which is null, not 0.
- `profit.logged.units` and `per_unit` are null when `n` is 0; `missing_price`
  counts graded games whose price for our side is missing. `profit.close`
  uses games with `clv_status = 'priced'` and a valid closing price for our side.
- `below_50_confidence_ids` lists graded games only.
- Two programs sum in different orders, so compare floats at about 1e-9, not
  for exact equality.
- `favorite_close` and `sequential.favorite_close` use only games with
  `clv_status = 'priced'` and both closing prices present; their `n` says how many.
- `sequential.market_brier` uses the games in `probability.n`, x = market
  (proportional, logged) Brier − model Brier per game.
- A window with `n = 0` is not emitted.
- Season types come from the game id; a graded game with no id is
  `unconfirmed`, and its season comes from its tip-off's Eastern date
  (October to September).
- `better_record` needs the league's game log; for an unconfirmed game it
  uses the season from the Eastern date and the game's Eastern date.
