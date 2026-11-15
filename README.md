# picks pipeline + hosted board

Two Render services (deployed via render.yaml-style env vars):
- `pipeline/` — ESB esoccer picks (BetsAPI), NBA model paper book, Kalshi esports recorder.
  Serves: `/` status, `/data` ledgers, `/board` bets for the hosted board, `/graded` settlements.
- `board/` — multi-user picks board (auth, place/skip/undo, auto-settlement).

Secrets are env-only: BETSAPI_KEY (pipeline), FIRST_ADMIN_* (board bootstrap).
