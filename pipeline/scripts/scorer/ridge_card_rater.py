"""ridge_card_rater.py — score the friend's LIVE card with the ridge engine, the
mirror of source_observer.py --live but deriving P(win)/EV from game mechanics
(player + brand skill, paired-residual enumeration) instead of classifying the bet.

For every NBA single on the source board's current card it writes:
  model_win = ridge P(win) at the posted line, ev = net EV per $1, bet = EV >= 0.
Output -> history/ridge_card.json (side-by-side with source_observer's scored_card.json)
so the two raters can be compared / ensembled on the SAME card.

Read-only vs the board: writes only history/ridge_card.json.
"""
import json, os, re, sys, datetime as dt
import numpy as np
import urllib.request, http.cookiejar
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ridge_engine as re_in
import source_observer as so

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "history", "ridge_card.json")
SOURCE_BASE = so.SOURCE_BASE
UA = so.UA or {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
               "Referer": "https://h2hggl.com/", "Origin": "https://h2hggl.com/"}

def walk():
    games = re_in.load_games(nba_only=True)
    fc = re_in.walk_forward(games, min_train=400, refit_every=50, lam=30.0)
    pool = {}
    for f in fc:
        key = tuple(sorted((f["a"], f["b"])))
        pool.setdefault(key, []).append((f["eA"], f["eB"]))
    by_pair = {}
    for f in fc:
        by_pair.setdefault(tuple(sorted((f["a"], f["b"]))), []).append(f)
    return games, fc, pool, by_pair

def fetch_card():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(urllib.request.Request(SOURCE_BASE + "/", headers=UA), timeout=20).read()
    card = json.loads(op.open(urllib.request.Request(SOURCE_BASE + "/data", headers=UA), timeout=25).read())
    bets = card.get("bets") if isinstance(card, dict) else (card or [])
    return [b for b in bets if b.get("kind") == "single" and b.get("league") == "nba"]

def forecast_for(fc, pool, pair, start_iso):
    """Most recent walk-forward forecast STRICTLY before the game's start (prior-only)."""
    cands = [f for f in fc if tuple(sorted((f["a"], f["b"]))) == pair]
    if not cands:
        return None, None
    bt = so.ts(start_iso)
    prior = [f for f in cands if f["start"] < (bt or dt.datetime.max.replace(tzinfo=dt.timezone.utc))]
    if not prior:
        # no prior game for this pair yet -> use the earliest forecast (cold start)
        target = cands[0]
    else:
        target = max(prior, key=lambda f: f["start"])
    return target, pool.get(pair, [])

def main():
    games, fc, pool, by_pair = walk()
    print("ridge walk-forward games:", len([f for f in fc]))
    card = fetch_card()
    print("friend card NBA singles:", len(card))
    out = []
    skipped = 0
    for b in card:
        pair = so.parse_pair(b.get("match") or "%s v %s" % (b.get("ga"), b.get("gb")))
        if not pair:
            skipped += 1; continue
        target, poolr = forecast_for(fc, pool, pair, b.get("start"))
        if target is None or not poolr:
            skipped += 1; continue
        probs = re_in.market_probs(target, poolr)
        if not probs:
            skipped += 1; continue
        pw, push = re_in.prob_for_pick(probs, b.get("side"))
        if pw is None:
            skipped += 1; continue
        try: pw = float(pw)
        except Exception: pw = None
        if pw is None:
            skipped += 1; continue
        ev = float(re_in.ev_from_prob(pw, push, b.get("price")))
        out.append({"key": b.get("key"), "match": b.get("match"), "side": b.get("side"),
                    "market": b.get("market"), "book": b.get("book"), "league": b.get("league"),
                    "price": b.get("price"), "stake": b.get("stake"),
                    "model_win": round(pw * 100, 1), "push_pct": round(float(push) * 100, 1),
                    "ev": round(ev * 100, 1), "bet": bool(ev >= 0.0)})
    out.sort(key=lambda x: -float(x.get("model_win") or 0))
    json.dump({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "engine": "ridge",
               "card": out, "skipped": skipped}, open(OUT, "w", encoding="utf-8"), indent=1)
    bet = [x for x in out if x.get("bet")]
    print("ridge-scored card:", len(out), "| bet (EV>=0):", len(bet), "| skipped:", skipped, "->", OUT)
    for x in out[:8]:
        print("   %s  %-22s p=%.1f%% ev=%+.1f%% %s" % (x["side"], x.get("match"), x["model_win"], x["ev"], "BET" if x["bet"] else ""))

if __name__ == "__main__":
    main()
