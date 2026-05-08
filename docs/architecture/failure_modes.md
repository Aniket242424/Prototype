# Failure Modes — Authoritative Response Matrix

Every module is built around the assumption that **its dependencies will fail**. This file enumerates the failures we plan for and the response we commit to.

| # | Failure | Detection | Response | Owner module |
|---|---|---|---|---|
| 1 | Upstox WS disconnect | heartbeat / no tick > 5s | Reconnect (exp backoff). Mark instrument stale. Risk Engine rejects entries on stale instruments. | Market Data |
| 2 | Daily token expiry (03:30 IST) | API 401 | TokenManager raises `TokenExpiredError`. All live-order paths halt. Operator runs `make auth`. | Auth |
| 3 | Postgres unreachable | connection error | Existing positions managed in-memory; new entries halted (no provenance = no trade). Alert. | Infrastructure |
| 4 | Redis unreachable | connection error | Halt new entries; degraded mode (no pubsub, no kill switch read). Treat kill switch as TRIPPED on read failure. Alert. | Infrastructure |
| 5 | Claude API down/timeout | timeout / 5xx | Skip AI scoring; downgrade `advisor_score` to 0.5 default. **Never block trading on AI.** | AI Reasoning |
| 6 | Broker rejects order | API response | Log full request+response. Do **not** retry rejected orders automatically. Alert. | Execution |
| 7 | Spread blowout mid-trade | live spread > 2× entry spread | Downgrade exit to MARKET. Log slippage anomaly. | Execution + Position |
| 8 | Stop-loss missed (price gapped past) | tick monitor | MARKET exit at next available price. Slippage logged with anomaly flag. | Position |
| 9 | Power/host loss | external | Positions remain at broker. Daily cap is the only protection. Operator runs recovery procedure on restart. | n/a |
| 10 | Time skew (host clock wrong) | startup check vs. NTP | Hard-fail at startup. Trading depends on accurate timestamps. | Core |
| 11 | Disk full | Postgres write fails | Same as #3 — halt new entries. | Infrastructure |
| 12 | Unhandled exception in any engine | global handler | Trip kill switch. Attempt emergency exit on any open position. Alert. | All |
| 13 | Acknowledgment file modified mid-session | SHA mismatch on next check | Lock #2/#3 fails — live orders blocked. Operator must re-sign. | Risk |
| 14 | Slippage anomaly cluster | slippage_log analytics | Auto-trip kill switch after `slippage_kill_consecutive` events. | Risk |
| 15 | Orphaned position (broker shows position we don't have in DB) | reconciliation pass | Halt entries. Alert with both sides of the discrepancy. Operator-only resolution. | Position |

## Recovery procedures

For each failure mode above, a runbook lives under `docs/runbooks/`. Operators do not improvise — they follow the runbook.
