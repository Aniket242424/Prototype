# Live-Trading Risk Acknowledgment

**Do not modify this file unless you accept the risks below in full. The system computes a SHA-256 of this file and records it on acknowledgment. Any modification invalidates the acknowledgment and disables live trading until re-signed.**

---

I, the operator of this Trading_Agent system, acknowledge and accept the following:

## 1. Capital risk
Options buying carries the risk of total loss of premium paid. Indian index options can move 50–90% in a single day. The conservative caps configured in `config/risk.yaml` reduce — but do not eliminate — the risk of significant capital loss.

## 2. System risk
This software is provided as-is. It has not been independently audited. Bugs, race conditions, or incorrect risk-engine logic may cause unintended orders, oversized positions, or failures to exit losing trades.

## 3. Connectivity risk
Loss of internet, broker API outage, WebSocket disconnection, or local power loss may prevent the system from exiting a position. The kill switch is local; if the host is unreachable, positions remain open at the broker.

## 4. Slippage and execution risk
Live execution will differ from backtest results. Indian options spreads can widen dramatically during volatility events. The slippage controls reduce average slippage but cannot prevent worst-case events.

## 5. Regulatory risk
The operator is solely responsible for compliance with SEBI regulations, broker terms of service, and applicable taxation. This system does not perform any compliance checks beyond basic order validation.

## 6. Daily token expiry
Upstox access tokens expire at 3:30 AM IST daily. If re-authentication is missed, the system enters a degraded state and cannot execute. Open positions at the broker remain at risk of adverse moves until re-auth completes.

## 7. AI advisor disclaimer
The Claude AI reasoning engine is an advisor, not a decision-maker. Its outputs are filtered through deterministic risk controls. The operator does not delegate execution authority to the AI.

## 8. Live-trading authorization scope
By signing this file via `scripts/acknowledge_live_trading.py`, I authorize the system to place real orders against the live Upstox account configured in `.env`, subject to the conservative caps in `config/risk.yaml`. I understand I can revoke this authorization at any time by setting `LIVE_TRADING=false` in `.env`, deleting the row in `acknowledgment_log`, or invoking the global kill switch.

---

**Conservative caps in effect (Phase 0 defaults — change in `config/risk.yaml`):**
- Daily max loss: 2.0% of configured capital
- Per-trade max risk: 0.5% of configured capital
- Max concurrent positions: 1
- Max trades/day: 3
- Consecutive-loss lockout: 2 losses disables trading until next session
- Slippage kill switch: 30bps × 3 consecutive trades

---

By running `python scripts/acknowledge_live_trading.py`, I confirm I have read and accepted the above.
