# Hosted picks board — run instructions (for the cloud agent or Render)

`board_host.py` is a multi-user hosted version of the picks board, for the
owner + a few friends. It displays the live card from the BRAIN (the pipeline
host's /data) and gives each user their own place/skip/undo tracking.

## Security model (already built and acceptance-tested)
- scrypt password hashing, unique salt per user; sessions are random tokens
  stored only as SHA-256 hashes; httpOnly + SameSite=Lax cookies; 14-day expiry
- no public registration; 5 failed logins locks the account for 10 minutes
- roles: admin (creates users via POST /act {act: admin_create_user, username,
  password}) and user (view card + track own bets only)
- first admin bootstraps from FIRST_ADMIN_USERNAME/FIRST_ADMIN_PASSWORD ONLY
  while the users table is empty — REMOVE those env vars after first login

## Setup
1. Env: CARD_SOURCE = URL of the pipeline host's /data (the brain),
   DB_PATH = a persistent path, PORT as assigned, and the two bootstrap vars.
2. `python board_host.py` — no dependencies beyond stdlib.
3. Log in as the admin, create one account per friend, share the URL +
  credentials privately. Never put real passwords in a repo.

## Settlement (built and tested)
A background thread pulls SETTLE_SOURCE every SETTLE_EVERY seconds (default
120) — a URL returning the graded ledger as {key: {result: win|loss|push,
pnl?: number}} or a list of such rows. Each user's placed bets are matched by
key, graded with ledger P&L (or P&L computed from their stake/price), and
`/data` serves `myrecord` per user: {w, l, p, pnl, pending}.

## Remaining v1 gap (cosmetic)
The UI is the original board's index.html; it renders the card + place/skip/
undo per user, but does not yet display `myrecord` — the data layer is done,
wiring a per-user record widget into the page is the last step.

## Acceptance tests (already green locally, 12/12)
test_board_host.py covers: auth redirect, login page, bad-password rejection,
session cookie, board serving, card + per-user mine isolation, place/persist/
undo, and admin-route blocking. Re-run it against any new deployment.
