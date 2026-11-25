#!/usr/bin/env python
"""esb_backfill.py — build the EsportsBattle training corpus from BetsAPI.

Pulls all ENDED events (with final scores) for the Esoccer Battle leagues the
books carry and appends them to esb_games.jsonl. One game per line:
  {id, league, league_id, time, home, away, home_alias, away_alias, ss,
   home_score, away_score}

Leagues: 22614 Esoccer Battle 8-min (2x4 leagues: World Cup A/B, Premier,
La Liga, Serie A, Bundesliga, Portugal, Europa), 38439 Battle Volta 6-min.
Safe to re-run: dedupes on BetsAPI event id.
"""
import json, os, re, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.environ.get('SCORER_HISTORY_DIR', HERE), 'esb_games.jsonl')
TOK = (os.environ.get('BETSAPI_KEY') or '').strip() or open(os.path.join(HERE, 'betsapi_key.txt'), encoding='utf-8').read().strip()
UA = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://betsapi.com/'}
LEAGUES = {22614: 'battle8', 38439: 'volta6', 37298: 'h2hggl8'}
MAX_PAGES = 40          # 50 per page -> up to 2000 games per league


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset = e.headers.get('x-ratelimit-reset')
                wait = 30.0
                if reset:
                    try:
                        wait = max(5.0, min(120.0, float(reset) - time.time()))
                    except Exception:
                        pass
                if i == tries - 1:
                    raise
                time.sleep(wait + (i * 3.0))
                continue
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def alias(name):
    m = re.search(r'\(([A-Za-z0-9]+)\)\s*$', (name or '').strip())
    return m.group(1).upper() if m else ''


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    have = set()
    if os.path.exists(OUT):
        for l in open(OUT, encoding='utf-8'):
            try:
                have.add(json.loads(l)['id'])
            except Exception:
                pass
    n_new = 0
    with open(OUT, 'a', encoding='utf-8') as f:
        for lid, lname in LEAGUES.items():
            got = 0
            for page in range(1, MAX_PAGES + 1):
                d = get(f'https://api.betsapi.com/v1/events/ended?token={TOK}'
                        f'&sport_id=1&league_id={lid}&page={page}')
                rows = d.get('results') or []
                if not rows:
                    break
                for e in rows:
                    eid = str(e.get('id'))
                    if eid in have:
                        continue
                    h, a = e.get('home') or {}, e.get('away') or {}
                    ss = e.get('ss') or ''
                    m = re.match(r'\s*(\d+)\s*-\s*(\d+)\s*$', ss)
                    if not (h.get('name') and a.get('name') and m):
                        continue
                    f.write(json.dumps({
                        'id': eid, 'league': lname, 'league_id': lid,
                        'time': e.get('time'),
                        'home': h.get('name'), 'away': a.get('name'),
                        'home_alias': alias(h.get('name')),
                        'away_alias': alias(a.get('name')),
                        'ss': ss, 'home_score': int(m.group(1)),
                        'away_score': int(m.group(2))}) + '\n')
                    have.add(eid)
                    n_new += 1
                    got += 1
                if len(rows) < 50:
                    break
                time.sleep(1.2)     # be polite to the API on his hotspot
            print(f'league {lid} ({lname}): +{got} new')
    total = len(have)
    print(f'corpus: {OUT}')
    print(f'new games: {n_new} | total in corpus: {total}')


if __name__ == '__main__':
    main()
