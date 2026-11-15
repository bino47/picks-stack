#!/usr/bin/env python
"""source_observer.py — learns the SOURCE board's model by observing it.

Idea: the friend's board posts *suggested* picks before anyone bets them. Every
suggested single is a sample of the other model's judgment. We grade ALL of them
(not just the ones we placed) against real H2H GG results, train a small model on
"did this suggestion win?", then score the board's CURRENT live card so we can say
which of today's picks sit in the profitable band.

READ-ONLY against picks-board and the source board. Writes only into
C:\\Users\\cbino\\picks-analysis\\history\\ (model + scored card).

Usage:
  python source_observer.py            # grade registry + train + save model
  python source_observer.py --live      # also fetch live /data and score the card
  python source_observer.py --score <picks.jsonl-ish>  # score a path's suggestions
"""
import json, math, os, re, sys
import datetime as dtm
from collections import defaultdict

BOARD_DIR = os.environ.get("SCORER_BOARD_DIR", r"C:\Users\cbino\picks-board")
OUT_DIR = os.environ.get("SCORER_DATA_ROOT", r"C:\Users\cbino\picks-analysis")
HIST = os.path.join(OUT_DIR, "history")
REGISTRY = os.path.join(HIST, "registry.jsonl")
INDEX = os.path.join(HIST, "real_games.json")
MODEL_FILE = os.path.join(HIST, "source_model.json")
SCORED_FILE = os.path.join(HIST, "scored_card.json")
SOURCE_BASE = "https://picks.001msk001.cc/p/ZKHxK1Wrkl88RjbayhBkSaUspEjM_fqw"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def ts(v):
    try:
        return dtm.datetime.fromisoformat((v or "").replace("Z", "+00:00").replace(" ", "T"))
    except Exception:
        return None


def parse_pair(m):
    rr = re.match(r"([A-Za-z0-9 ]+)\s+v\s+([A-Za-z0-9 ]+)", m or "")
    return tuple(sorted([rr.group(1).strip().upper(), rr.group(2).strip().upper()])) if rr else None


def load_index():
    try:
        return json.load(open(INDEX, encoding="utf-8"))
    except Exception:
        return {"by_date": {}}


def grade(side, score):
    """Return 'win'/'loss'/'push'/None for a side vs a real score."""
    s = (side or "").strip()
    m = re.match(r"(?i)^(Over|Under)\s+([\d.]+)$", s)
    if m:
        tot = sum(score); over = m.group(1).lower() == "over"; line = float(m.group(2))
        if tot > line: return "win" if over else "loss"
        if tot < line: return "loss" if over else "win"
        return "push"
    m2 = re.match(r"(?i)^([A-Za-z0-9 ]+)\s+([+-])([\d.]+)$", s)
    if m2:
        return None  # spread needs alias position; caller resolves
    return None


def nearest_game(real_by_pair, pair, bet_start):
    bt = ts(bet_start); best = None; bd = None
    for t, g in real_by_pair.get(pair, []):
        d = abs((t - bt).total_seconds()) if bt else 0
        if best is None or d < bd:
            best, bd = g, d
    return best


def spread_result(g, alias):
    if alias not in g["aliases"]:
        return None
    i = g["aliases"].index(alias); o = 1 - i
    m2 = re.match(r"(?i)^([A-Za-z0-9 ]+)\s+([+-])([\d.]+)$", alias)
    return None


def grade_row(r, g):
    res = grade(r.get("side") or r.get("pick") or "", g["score"])
    if res:
        return res
    m2 = re.match(r"(?i)^([A-Za-z0-9 ]+)\s+([+-])([\d.]+)$", (r.get("side") or r.get("pick") or "").strip())
    if m2 and m2.group(1).strip().upper() in g["aliases"]:
        i = g["aliases"].index(m2.group(1).strip().upper()); o = 1 - i
        marg = g["score"][i] - g["score"][o]
        adj = marg + (float(m2.group(3)) if m2.group(2) == "+" else -float(m2.group(3)))
        return "win" if adj > 0 else ("push" if abs(adj) < 1e-9 else "loss")
    return None


def build_dataset():
    """Grade every suggested single in the registry against real results."""
    index = load_index()
    real_by_pair = defaultdict(list)
    for d, gs in index.get("by_date", {}).items():
        for g in gs:
            t = ts(g.get("start"))
            if t and sum(g["score"]) >= 45:
                real_by_pair[tuple(g["aliases"])].append((t, g))
    records = []
    if not os.path.exists(REGISTRY):
        return records   # no registry yet (fresh host): scoring still works with the frozen model
    with open(REGISTRY, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "single" or r.get("league") != "nba":
                continue
            if r.get("status") not in ("open", "missed", "placed"):
                continue
            pair = parse_pair(r.get("match"))
            if not pair:
                continue
            g = nearest_game(real_by_pair, pair, r.get("start"))
            if g is None:
                continue
            res = grade_row(r, g)
            if res in ("win", "loss"):
                records.append({
                    "side": r.get("side"), "market": r.get("market"),
                    "price": float(r.get("price") or -110),
                    "stake": float(r.get("stake") or 0),
                    "book": r.get("book"), "start": r.get("start"),
                    "result": 1 if res == "win" else 0,
                })
    return records


def feats(r):
    price = float(r.get("price") or -110)
    stake = float(r.get("stake") or 0)
    return [
        1.0,
        1.0 if r.get("market") in ("Total", "totals") else 0.0,
        1.0 if (r.get("side") or "").lower().startswith("over") else 0.0,
        price / 200.0,
        1.0 if stake >= 100 else 0.0,
        1.0 if r.get("book") == "betmgm" else 0.0,
    ]


def train_logit(X, y, iters=2500, lr=0.4):
    w = [0.0] * len(X[0]); m = len(X)
    for _ in range(iters):
        grad = [0.0] * len(w)
        for xi, yi in zip(X, y):
            z = max(min(sum(a * b for a, b in zip(w, xi)), 30), -30)
            sig = 1 / (1 + math.exp(-z)); err = yi - sig
            for j in range(len(w)):
                grad[j] += err * xi[j]
        for j in range(len(w)):
            w[j] += lr * grad[j] / m
    return w


def prob(w, r):
    z = sum(a * b for a, b in zip(w, feats(r)))
    return 1 / (1 + math.exp(-z))


def american_to_decimal(price):
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 2.0
    return 1.0 + p / 100.0 if p > 0 else 1.0 + 100.0 / abs(p)


def ev_per_dollar(p_win, price):
    """EV per $1 staked, in dollars. >0 means the model thinks it's +EV."""
    try:
        return (p_win * american_to_decimal(price)) - 1.0
    except Exception:
        return None


def load_model():
    try:
        return json.load(open(MODEL_FILE, encoding="utf-8"))
    except Exception:
        return None


def score_live_card():
    """Fetch the source board's CURRENT card and score each suggestion."""
    import urllib.request, http.cookiejar
    try:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(urllib.request.Request(SOURCE_BASE + "/", headers=UA), timeout=20).read()
        card = json.loads(op.open(urllib.request.Request(SOURCE_BASE + "/data", headers=UA), timeout=25).read())
    except Exception as e:
        print("live fetch failed:", e)
        return
    m = load_model()
    if not m:
        print("no model file; run plain first")
        return
    w = m["weights"]
    bets = card.get("bets") if isinstance(card, dict) else (card or [])
    out = []
    for b in bets or []:
        if b.get("kind") != "single":
            continue
        p = prob(w, b)
        ev = ev_per_dollar(p, b.get("price"))
        mstr = b.get("match") or ((b.get("ga") and b.get("gb")) and "%s v %s" % (b["ga"], b["gb"])) \
               or re.sub(r"^(?:[A-Za-z0-9]+T\d{2}:\d{2}:\d{2}\.\d{3}Z_|[^_]+_)", "", (b.get("key") or ""))[:40]
        out.append({"key": b.get("key"), "match": mstr, "side": b.get("side"),
                    "market": b.get("market"), "book": b.get("book"), "league": b.get("league"),
                    "price": b.get("price"), "stake": b.get("stake"),
                    "model_win": round(p * 100, 1), "ev": (round(ev * 100, 1) if ev is not None else None),
                    "bet": p >= 0.64})   # historically-profitable band
    out.sort(key=lambda x: -float(x.get("model_win") or 0))
    with open(SCORED_FILE, "w", encoding="utf-8") as f:
        json.dump({"at": dtm.datetime.now(dtm.timezone.utc).isoformat(),
                   "model_trained": m.get("trained"), "card": out}, f, indent=1)
    print("scored live card:", len(out), "suggestions ->", SCORED_FILE)
    bet_this = [x for x in out if x.get("bet")]
    print("  in profitable band (>=64%):", len(bet_this))
    grade_model(m)


GRADE_LOG = os.path.join(HIST, "model_graded.jsonl")
GRADE_HIST = os.path.join(HIST, "model_accuracy.json")


def grade_model(m):
    """Grade the model's own past predictions against real results.

    Read the last scored card, find the same single(s) settled in real_games,
    append win/loss + prediction to model_graded.jsonl, and write a rolling
    accuracy/EV summary to model_accuracy.json. Pure measurement — changes nothing.
    """
    if not os.path.exists(SCORED_FILE):
        return
    try:
        prev = json.load(open(SCORED_FILE, encoding="utf-8"))
    except Exception:
        return
    index = load_index()
    real_by_pair = defaultdict(list)
    for d, gs in index.get("by_date", {}).items():
        for g in gs:
            t = ts(g.get("start"))
            if t:
                real_by_pair[tuple(g["aliases"])].append((t, g))
    rows = []
    for c in prev.get("card") or []:
        side = c.get("side") or ""
        pair = parse_pair(c.get("match")) or parse_pair(side) or \
            (lambda s: (lambda m: tuple(sorted([m.group(1).strip().upper(), m.group(2).strip().upper()]))
                        if m else None)(re.match(r"([A-Za-z0-9 ]+)\s+v\s+([A-Za-z0-9 ]+)", s)))(side)
        if not pair:
            continue
        g = nearest_game(real_by_pair, pair, c.get("start") if c.get("start") else prev.get("at"))
        if g is None:
            continue
        res = grade_row(c, g)
        if res not in ("win", "loss"):
            continue
        predicted_win = c.get("model_win", 50) / 100.0
        rows.append({
            "at": dtm.datetime.now(dtm.timezone.utc).isoformat(timespec="seconds"),
            "match": c.get("match"), "side": side, "book": c.get("book"),
            "model_win": c.get("model_win"), "ev": c.get("ev"),
            "suggested_bet": 1 if c.get("bet") else 0,
            "actual": 1 if res == "win" else 0, "was_right": (res == "win") == (predicted_win >= 0.64),
        })
    if not rows:
        return
    with open(GRADE_LOG, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # rolling summary
    vals = []
    with open(GRADE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    vals.append(json.loads(line))
                except Exception:
                    pass
    n = len(vals)
    if not n:
        return
    right = sum(1 for v in vals if v.get("was_right"))
    wins = sum(1 for v in vals if v.get("actual") == 1 and v.get("suggested_bet"))
    bets = sum(1 for v in vals if v.get("suggested_bet"))
    # calibration: model said win>=0.64 -> how often right
    summary = {
        "graded": n, "accuracy": round(100.0 * right / n, 1),
        "suggested_bets": bets,
        "suggested_win_rate": (round(100.0 * wins / bets, 1) if bets else None),
        "last": vals[-1] if vals else None,
        "at": dtm.datetime.now(dtm.timezone.utc).isoformat(timespec="seconds"),
    }
    with open(GRADE_HIST, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    print("model self-grade: %d graded, acc %.1f%%, suggested-bets W=%.1f%% (n=%d)"
          % (n, 100.0 * right / n, (100.0 * wins / bets if bets else 0), bets))
    # rolling window accuracy (last 30)
    recent = vals[-30:]
    rw = sum(1 for v in recent if v.get("was_right"))
    print("  last-30 accuracy: %.1f%%" % (100.0 * rw / len(recent)))


def main():
    records = build_dataset()
    if len(records) < 50:
        print("not enough graded source suggestions:", len(records))
        return
    nwin = sum(r["result"] for r in records)
    print("source NBA suggestions graded:", len(records),
          "| win rate %.1f%%" % (100.0 * nwin / len(records)))

    # time-ordered split
    records.sort(key=lambda r: ts(r.get("start")) or dtm.datetime.min)
    cut = int(len(records) * 0.7)
    train, test = records[:cut], records[cut:]
    w = train_logit([feats(x) for x in train], [x["result"] for x in train])

    # holdout: rank & report
    scored = [(prob(w, x), x) for x in test]
    scored.sort(key=lambda q: q[0], reverse=True)
    acc = sum(1 for _, x in scored if (prob(w, x) > 0.5) == bool(x["result"])) / len(scored)
    half = len(scored) // 2
    top = scored[:half]; bot = scored[half:]
    topw = 100.0 * sum(x["result"] for _, x in top) / len(top)
    botw = 100.0 * sum(x["result"] for _, x in bot) / len(bot)
    print("holdout n=%d | accuracy %.1f%% | top-50%% win %.1f%% vs bottom %d..%.1f%%"
          % (len(scored), 100 * acc, topw, 0 if False else len(bot), botw))

    # top-decile composition
    from collections import Counter
    td = scored[: max(1, len(scored) // 10)]
    print("model's top-picks prefer:",
          dict(Counter(x["market"] for _, x in td)),
          dict(Counter('OVER' if (x["side"] or "").lower().startswith("over") else "UNDER" for _, x in td)))

    model = {"weights": w, "trained": dtm.datetime.now(dtm.timezone.utc).isoformat(),
             "n_train": len(train), "n_test": len(test),
             "holdout_acc": round(100 * acc, 1),
             "top50_win": round(topw, 1), "bottom50_win": round(botw, 1)}
    with open(MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(model, f, indent=1)
    print("model saved ->", MODEL_FILE)


if __name__ == "__main__":
    if "--live" in sys.argv:
        load_model() or main()
        score_live_card()
    else:
        main()
