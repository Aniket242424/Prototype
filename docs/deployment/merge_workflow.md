# Deployment workflow — merging `discussion` → `trading-agent`

## Current "deployment" model

There is no production server yet (Oracle Cloud setup was deferred). The
`trading-agent` branch is treated as the **canonical production-ready code**.
When a real server is set up in the future (Phase 4-6), the deployment
script will pull from this branch.

For now, "deploy" = **merge from a feature branch into `trading-agent`** on GitHub,
gated by CI passing + live-validation passing.

## The two gates

A change can only merge into `trading-agent` after BOTH:

| Gate | What it checks | How it's enforced |
|---|---|---|
| **1. Unit tests** | All pytest tests pass on the feature branch | GitHub Actions runs on every push; PR shows green/red |
| **2. Live validation** | Phase 2 produces sensible regime/intel/opportunity output during live market hours | Manual operator validation (you watch dashboard + report results) |

Gate 1 is automatic. Gate 2 is human judgment — there's no automated way to
say "the regime classifier is correctly identifying TREND_UP."

## Standard workflow

```text
1. Work on a feature branch (currently `discussion`, will be `phase-3`, etc.)
2. Push commits to that branch
3. GitHub Actions runs unit tests automatically
4. If tests fail → CI shows red → fix and re-push
5. When ready, also do live-validation during market hours
6. When BOTH gates pass:
     - Open a Pull Request: feature-branch → trading-agent
     - Review the diff
     - Merge (squash or merge commit, your call)
7. trading-agent branch now has the new code
```

## How to verify CI is green

After pushing to any branch, visit:

**https://github.com/Aniket242424/Prototype/actions**

You'll see a list of test runs. Latest commit shows ✅ (green) or ❌ (red).

You can also check directly on the commit page:
**https://github.com/Aniket242424/Prototype/commits/discussion**

Each commit shows the CI status icon next to it.

## How to create a PR

On GitHub:

1. Visit https://github.com/Aniket242424/Prototype
2. Click the branches dropdown → pick the feature branch (e.g., `discussion`)
3. Click "Pull request" button at the top right of the file list
4. Set base: `trading-agent`, compare: `discussion`
5. Add a description summarizing what's changing
6. Click "Create pull request"
7. Wait for CI to finish (usually 2-3 minutes)
8. If green: click "Merge pull request"

## What CI checks today

- **Unit tests** (`pytest tests/unit/`) — must all pass
- **Ruff lint** — runs but doesn't block merge yet (warnings only)

Future additions (Phase 3+):
- Integration tests with real Postgres + Redis containers
- Coverage thresholds
- Type checking (mypy strict)
- Backtest expectancy threshold (Phase 5+)

## When you DON'T need this workflow

Trivial changes you can commit directly to `trading-agent`:
- README typos
- Documentation-only changes
- Comment-only changes in code

Everything else: feature branch → CI green → PR → merge.

## When you DO need it

Anything that touches:
- Logic in any engine (Risk, Execution, Strategy, Regime, Intel, Opportunity)
- Risk caps in `config/risk.yaml`
- Order placement code (Phase 3+)
- Live-trading 3-lock gate
- Strike selection logic
- Database migrations

These changes affect real-money behavior (eventually) — they MUST go through CI + validation.
