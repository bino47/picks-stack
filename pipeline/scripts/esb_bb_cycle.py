#!/usr/bin/env python
"""esb_bb_cycle.py — eBasketball GG 4x5 stream: delta backfill -> model edges
vs MGM real lines -> picker pick/settle/report. One cycle, every 10 min.

Ledger: $SCORER_HISTORY_DIR/esb_bb_paper_ledger.jsonl (real recorded odds).
Edges:  $SCORER_HISTORY_DIR/esb_bb_edges.jsonl (p_model vs MGM americanOdds).
"""
import json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR') or os.path.join(HERE, 'history')
PY = sys.executable
TOK = (os.environ.get('BETSAPI_KEY') or '').strip()
CORPUS = os.path.join(HIST, 'esb_bb_games.jsonl')
UA = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://betsapi.com/'}


def get(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == 2:
                return {}
            time.sleep(2)


def delta_backfill(pages=3):
    """fetch newest ended events, append unseen to the corpus"""
    seen = {}
    if os.path.exists(CORPUS):
        for l in open(CORPUS):
            try:
                r = json.loads(l); seen[r['id']] = r
            except Exception:
                pass
    new = 0
    for page in range(1, pages + 1):
        d = get(f'https://api.betsapi.com/v1/events/ended?token={TOK}'
                f'&sport_id=18&league_id=25067&page={page}')
        rs = d.get('results') or []
        if not rs:
            break
        for r in rs:
            if r['id'] in seen or not r.get('ss') or '-' not in r['ss']:
                continue
            try:
                sa, sb = (int(x) for x in r['ss'].split('-'))
            except Exception:
                continue
            row = {'id': r['id'], 'time': int(r['time']), 'league': 'bb_gg_4x5',
                   'home': (r.get('home') or {}).get('name'),
                   'away': (r.get('away') or {}).get('name'),
                   'home_score': sa, 'away_score': sb}
            seen[row['id']] = row
            new += 1
        time.sleep(0.5)
    if new or not os.path.exists(CORPUS):
        with open(CORPUS, 'w') as f:
            for r in sorted(seen.values(), key=lambda x: x['time']):
                f.write(json.dumps(r) + '\n')
    return new, len(seen)


def run(script, args):
    r = subprocess.run([PY, os.path.join(HERE, script)] + args,
                       capture_output=True, text=True, timeout=240,
                       env=dict(os.environ, SCORER_HISTORY_DIR=HIST))
    out = (r.stdout or '').strip()
    print(f'[bb:{script}] rc={r.returncode} {out[:600]}', flush=True)
    if r.returncode != 0 and r.stderr:
        print(f'[bb:{script}] ERR {r.stderr[-400:]}', flush=True)


def main():
    # cold start (fresh Render disk): walk deep so per-gamer counts clear the
    # 25-game thin-sample guard (an 800-game walk leaves most gamers thin and
    # every fixture gets skipped). League has 250k+ ended events, so paging
    # is deep; ~45 pages ~= 2250 games ~= the VPS corpus.
    try:
        n_games = sum(1 for _ in open(CORPUS, encoding='utf-8'))
    except Exception:
        n_games = 0
    new, total = delta_backfill(45 if n_games < 2000 else 3)
    print(f'bb backfill: +{new} games, corpus {total}', flush=True)
    run('esb_bb_edges.py', [])
    run('esb_bb_picker.py', ['pick'])
    run('esb_bb_picker.py', ['settle'])
    run('esb_bb_picker.py', ['report'])


if __name__ == '__main__':
    main()
