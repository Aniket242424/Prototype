# Runbook — Daily Upstox Token Refresh

**Frequency:** every trading morning, before 09:15 IST.

Upstox access tokens expire at **03:30 IST every day**. The system enters a degraded state once the token expires; live order paths refuse to fire (`LiveTradingNotAuthorized` is raised on the very first check).

## Standard procedure

```bash
make auth
```

This:
1. Prints (and opens) the Upstox authorize URL.
2. After you log in, Upstox redirects to `UPSTOX_REDIRECT_URI?code=...`.
3. You paste the URL or just the `code` value into the prompt.
4. The CLI exchanges the code for a token and persists it (encrypted) to Postgres.

## Verify

```bash
make probe
```

Expect ✓ on all checks. Anything failing here means downstream modules will also fail.

## What if I miss the morning refresh?

- New entries are blocked (Risk Engine fails at lock #1 of the live-trading gate).
- Existing positions remain at the broker. The system still has the position record but cannot manage them via API until token is refreshed.
- This is exactly the situation `ACKNOWLEDGMENT.md §6` warns about. Operator owns this risk.

## Recovery if `make auth` itself fails

1. Check `.env` — `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI` all set?
2. Check Upstox developer portal — app status, redirect URI exactly matches?
3. Check Postgres — `docker compose ps postgres` healthy?
4. Inspect last successful auth: `SELECT * FROM tokens ORDER BY updated_at DESC LIMIT 1;`
5. If still failing, trip the kill switch and alert before market open.
