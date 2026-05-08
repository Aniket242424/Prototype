# Module Contracts (cross-reference)

This file is a stable reference of the **typed boundaries** between modules. ARCHITECTURE.md describes intent; this file describes shape.

> Phase 0 ships only the auth/infrastructure types. Domain DTOs below are reserved for Phase 1+; they are listed here so any code touching adjacent modules speaks the same vocabulary from day one.

```python
# market_data (Phase 1)
class Tick(BaseModel):
    instrument_key: str
    ts: datetime
    ltp: Decimal
    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int | None
    ask_qty: int | None
    volume: int | None
    oi: int | None

class StrikeLevel(BaseModel):
    strike: Decimal
    ce_ltp: Decimal | None; ce_iv: Decimal | None; ce_oi: int | None; ce_bid: Decimal | None; ce_ask: Decimal | None
    pe_ltp: Decimal | None; pe_iv: Decimal | None; pe_oi: int | None; pe_bid: Decimal | None; pe_ask: Decimal | None

class ChainSnapshot(BaseModel):
    underlying: str
    expiry: date
    ts: datetime
    underlying_spot: Decimal
    strikes: list[StrikeLevel]

# regime (Phase 2)
class RegimeState(BaseModel):
    underlying: str
    regime: Regime
    confidence: float                 # 0..1
    components: dict[str, float]       # atr, adx, rv5, rv15, vix_delta, basis, ...
    ts: datetime

# opportunity (Phase 2)
class Opportunity(BaseModel):
    underlying: str
    direction: Direction
    score: float                       # 0..1
    components: dict[str, float]       # 9 named scores
    recommended_expiry: date
    recommended_strike_band: dict[str, Decimal]
    ts: datetime

# strategy (Phase 4)
class TradeIntent(BaseModel):
    strategy_name: str
    underlying: str
    instrument_key: str                # the option contract
    side: OrderSide                    # always BUY in Phase 4 baseline
    option_type: OptionType            # CE / PE
    strike: Decimal
    expiry: date
    target_qty: int                    # in contracts (lots × lot_size)
    target_premium: Decimal
    stop_premium: Decimal
    profit_target_premium: Decimal
    confidence: float

# ai_reasoning (Phase 4) — strict JSON
class AdvisorOutput(BaseModel):
    decision: Literal["CALL", "PUT", "NO_TRADE"]
    confidence: float
    rationale: str
    warnings: list[str]
    advisor_score: float

# risk (Phase 3)
class RiskDecision(BaseModel):
    approved: bool
    reason: str
    code: str                          # see docs/architecture/risk_design.md
    sized_qty: int | None
    max_premium: Decimal | None
    inputs_snapshot: dict

# execution (Phase 3)
class ExecutionResult(BaseModel):
    order_id: int
    status: OrderStatus
    fills: list[Fill]
    realized_slippage_bps: float
    reference_mid: Decimal
```

These types are the **load-bearing seams**. Changing any of them requires a migration plan + a corresponding Alembic revision if the change touches persisted columns.
