#!/usr/bin/env python
"""betsapi_feed.py — BetsAPI fallback/bridge for the H2H GG results feed.

api-h2h.cdn-hudstats.com is Cloudflare-blocked (403) from datacenter IPs since
2026-11-13, which froze real_games.json and left every board pick unresolved.
This rebuilds the missing days straight from BetsAPI ended events:

  sport 18  league 25067  -> NBA-sim GG 4x5 (league tag "nba")
  sport 1   leagues 22614 (battle8) / 38439 (volta6) / 37298 (h2hggl8) -> fifa

Output schema matches h2hggl_recorder.py EXACTLY (that matters):
  aliases  = sorted([participantA, participantB]) upper   (sorted!)
  score    = [teamAScore, teamBScore]                     (A/B feed order)
  teams    = (teamAName, teamBName)                       (A/B feed order)
Merged into $SCORER_HISTORY_DIR/history/real_games.json (existing rows kept).

  python betsapi_feed.py --days 4 [--pages 6]
"""
import json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR') or os.path.join(HERE, 'history')
# the feed file lives at <root>/history/real_games.json; the scheduler may hand
# us either the root or the history dir as SCORER_HISTORY_DIR
_FEED_DIR = HIST if os.path.basename(HIST.rstrip('/')) == 'history' else os.path.join(HIST, 'history')
OUT = os.path.join(_FEED_DIR, 'real_games.json')
TOK = (os.environ.get('BETSAPI_KEY') or '').strip()
UA = {'User-Agent': 'Mozilla/5.0'}

# BetsAPI league_id -> feed sport tag
LEAGUES = [(18, 25067, 'nba'), (1, 22614, 'fifa'), (1, 38439, 'fifa'), (1, 37298, 'fifa')]
NICK = {22614: 'battle8', 38439: 'volta6', 37298: 'h2hggl8', 25067: 'bb_gg_4x5'}


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


def days_back(n):
    return [time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400 * i)) for i in range(n)]


def fetch(days, pages):
    """ended events -> {day: [feed rows]}; walks pages until older than wanted"""
    by_date = {}
    for sport, lid, tag in LEAGUES:
        got_any = True
        page = 1
        oldest_want = min(days)
        while got_any and page <= pages * len(days):
            d = get(f'https://api.betsapi.com/v1/events/ended?token={TOK}'
                    f'&sport_id={sport}&league_id={lid}&page={page}')
            rs = d.get('results') or []
            if not rs:
                break
            hit_older = False
            for r in rs:
                if not r.get('ss') or '-' not in str(r['ss']):
                    continue
                try:
                    sa, sb = (int(x) for x in str(r['ss']).split('-'))
                except Exception:
                    continue
                st = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(r['time'])))
                if st[:10] < oldest_want:
                    hit_older = True
                    continue
                if st[:10] not in days:
                    continue
                hm = (r.get('home') or {}).get('name') or ''
                aw = (r.get('away') or {}).get('name') or ''
                mh = re.match(r'^(.*?)\s*\((.+?)\)$', hm)
                ma = re.match(r'^(.*?)\s*\((.+?)\)$', aw)
                if not (mh and ma):
                    continue
                by_date.setdefault(st[:10], []).append({
                    'aliases': sorted([mh.group(2).upper(), ma.group(2).upper()]),
                    'score': [sa, sb],
                    'teams': (mh.group(1), ma.group(1)),
                    'status': 'MATCH_ENDED', 'start': st,
                    'src': 'betsapi:' + NICK[lid]})
            if hit_older:
                break  # walked past the wanted window
            page += 1
            time.sleep(0.4)
        time.sleep(0.3)
    return by_date


def main():
    days = 4
    pages = 6
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])
    if '--pages' in sys.argv:
        pages = int(sys.argv[sys.argv.index('--pages') + 1])
    want = set(days_back(days))
    # ephemeral-disk hosts (Render) start with an empty feed after each deploy;
    # a shallow fetch would strand picks older than the first cycle's window.
    try:
        existing = sum(len(v) for v in json.load(open(OUT, encoding='utf-8')).get('by_date', {}).values())
    except Exception:
        existing = 0
    if existing < 5000:
        pages = max(pages, 16)   # cold start: walk deep enough to fill days
        print(f'betsapi_feed: cold feed ({existing} rows) -> deep backfill')
    try:
        cur = json.load(open(OUT, encoding='utf-8'))
    except Exception:
        cur = {'by_date': {}}
    merged = 0
    sig_seen = set()
    for day, games in cur.get('by_date', {}).items():
        for g in games:
            sig_seen.add((day, tuple(g.get('aliases') or []), g.get('start')))
    new_by_date = fetch(sorted(want), pages)
    for day, games in new_by_date.items():
        bucket = cur['by_date'].setdefault(day, [])
        for g in games:
            sig = (day, tuple(g['aliases']), g['start'])
            if sig in sig_seen:
                continue
            # a feed row with the same aliases+start but a placeholder score
            # (e.g. 0-0 pending) gets upgraded to the real result
            replaced = False
            for i, old in enumerate(bucket):
                if (tuple(old.get('aliases') or []) == tuple(g['aliases'])
                        and old.get('start') == g['start']
                        and old.get('score') in ([0, 0], None)):
                    bucket[i] = g
                    replaced = True
                    merged += 1
                    break
            if not replaced:
                bucket.append(g)
                sig_seen.add(sig)
                merged += 1
    cur['by_date'] = {k: v for k, v in sorted(cur['by_date'].items())}
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cur, f)
    os.replace(tmp, OUT)
    print(f'betsapi_feed: +{merged} rows across {len(new_by_date)} days; '
          f'total {sum(len(v) for v in cur["by_date"].values())}')


if __name__ == '__main__':
    main()
