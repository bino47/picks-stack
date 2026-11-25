#!/bin/bash
# Render entrypoint: install deps, seed data (copy-only-if-missing), start scheduler.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DATA_ROOT="${DATA_ROOT:-/var/data}"
export SCORER_HISTORY_DIR="$DATA_ROOT"
export BETSAPI_KEY="${BETSAPI_KEY:-}"
mkdir -p "$DATA_ROOT/history"
# seed state copy-only-if-missing (never clobber live pipeline writes)
# NOTE: model_paper_trader reads $SCORER_HISTORY_DIR/history/{scored_card,real_games}.json
# while esb_* + kalshi_* read $SCORER_HISTORY_DIR directly — seed both locations.
if [ -d "$ROOT/seed" ]; then
  for f in "$ROOT"/seed/*; do
    base="$(basename "$f")"
    case "$base" in
      scored_card.json|real_games.json) DEST="$DATA_ROOT/history/$base" ;;
      manual_graded.jsonl)              DEST="$DATA_ROOT/$base"; cp "$f" "$DEST"; continue ;;
      *)                                DEST="$DATA_ROOT/$base" ;;
    esac
    [ -e "$DEST" ] || cp "$f" "$DEST"
  done
fi
# the trained scorer model must exist in the data dir for --live scoring
[ -e "$DATA_ROOT/history/source_model.json" ] || \
  cp "$ROOT/scripts/scorer/source_model.json" "$DATA_ROOT/history/source_model.json" 2>/dev/null || true
pip install --quiet numpy curl_cffi
exec python "$ROOT/scripts/scheduler.py"
