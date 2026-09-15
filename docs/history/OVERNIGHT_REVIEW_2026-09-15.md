# Gemini overnight paper review — September 15, 2026

## Outcome

The primary hardened observer stopped gracefully at 08:02:29 America/New_York at the owner's direction, 57 minutes before its original 09:00 deadline. No observer process remains. All three SQLite databases pass `PRAGMA quick_check`; all storage buffers ended empty; no storage-overflow drops, cycle-budget skips, REST throttles, or supervisor restarts were recorded.

No order was submitted by any paper observer. Possible trade touches, hypothetical markouts, and sampled qualifying geometry are not fills, realized profit, earned rewards, or venue-certified uptime.

## Run boundaries

| Run | Role | Actual observation span | Contracts | Unique reward pools | Snapshots | Notes |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `overnight-20260914` | Original pilot | about 23:30–23:40 ET | 10 | 10 | 100 | Stopped after expanded replacement was verified |
| `overnight-expanded-20260914` | Expansion check | about 23:39–23:49 ET | 50 | 25 | 500 | Overlaps the pilot briefly; not added to primary evidence |
| `overnight-hardened-20260914` | Primary evidence | 23:49–08:02 ET | 50 | 25 | 24,599 | Same cohort, hardened recovery path |

The runs are preserved separately. Their overlapping windows are not treated as independent observations or concatenated into a larger reward-uptime claim.

## Primary hardened run

- Coverage: 24,599 successful snapshots from 24,650 attempts (99.793%); 51 missing samples (0.207%).
- Reward geometry: 24,057 market-eligible snapshots and 24,049 two-tick-primary qualifying geometries. The two-tick geometry qualified in 97.764% of successful snapshots.
- Exit depth: 24,559 snapshots (99.837%) showed full displayed depth for a 10-contract exit on both outcomes. This is a point-in-time book observation, not guaranteed execution.
- Storage: 259,796 durable records; zero pending records and zero recorded overflow drops.
- Transport: 24,700 WebSocket requests, 222,191 frames, 192,361 ticker updates, 306 paced REST calls, and no REST throttles.
- Recovery: five errors around 00:47 ET (one socket close, one sampling connection error, and three invalid WebSocket messages) led to a second WebSocket generation. The single `observer_session` record has `restart: 0`, so the in-process supervisor never restarted the observer.
- CPU: 0.503% average of one logical core; 1.223% peak sampled interval.
- Pool accounting: 50 sibling contracts represented only 25 distinct reward pools. Shared pools are counted once.

One contract, `GEMI-HOUSE26IA-02-REPUBLICANNOMINEE`, had zero qualifying two-tick snapshots and an approximately 20.08-cent mean spread. It must not be treated as a viable primary quote merely because its sibling belongs to a rewarded pool.

## Quote-distance comparison

Possible trade touches during completed quote intervals were:

| Offset | Intervals | YES touches | NO touches | Either side | Either-side rate |
| ---: | ---: | ---: | ---: | ---: | ---: |
| Touch | 24,460 | 19 | 24 | 43 | 0.176% |
| One tick behind | 24,452 | 4 | 4 | 8 | 0.033% |
| Two ticks behind | 24,452 | 2 | 1 | 3 | 0.012% |

No interval had both outcomes touched. These are market trades through a hypothetical quote, not proof the bot would have filled at its queue position.

The two-tick hypothetical marks were roughly $0.30–$0.33 per separately assumed 10-contract side at 1, 5, and 15 minutes, versus roughly $0.10–$0.13 at the touch. That difference is largely mechanical because the hypothetical entry is two cents cheaper; the sharply lower touch rate shows that the wider price would also be much less likely to execute. The data supports two ticks as a cautious low-fill starting point, but cannot establish superior net reward yield.

## What the run establishes

- One WebSocket plus paced REST discovery can monitor 50 contracts with very low CPU and no observed rate-limit pressure.
- The 10-contract depth requirement was usually available in this cohort.
- Two ticks behind generally stayed inside the locally known reward geometry while producing far fewer possible touches than quoting at the touch.
- The recovery and storage hardening handled one connection disruption without data-buffer loss or a process restart.

## What remains unknown

- The run covered about 8 hours 13 minutes and crossed midnight. It cannot prove the published 50% full-day reward-uptime condition.
- No authenticated daily reward amount, pool-level score share, queue priority, realized fill rate, fee, rebate, or earned reward was measured.
- Public maker counts are not competitor score weights, and Gemini's exact final quadratic allocation curve remains unavailable.
- Hypothetical positive marks cannot be combined across YES and NO or reported as profit.

The paper evidence is sufficient to support the current scalable WebSocket-first architecture and a cautious two-tick research baseline. It is not sufficient by itself to claim profitability or expected reward dollars.
