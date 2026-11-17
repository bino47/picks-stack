#!/usr/bin/env python
"""esb_bb_edges.py — model vs MGM/Borgata real pre-match lines for eBasketball GG 4x5.

Model = per-gamer ridge (esb_bb_picker) score-pair distribution.
Prices = MGM cds-api (curl_cffi safari), sportId 119, fixture-view per fixture.
Outputs one JSON line per scan to history/esb_bb_edges.jsonl + stdout table of
EV-ranked edges. This is the Kelly input: p_model vs real americanOdds.

  python esb_bb_edges.py
"""
import json, os, re, sys, time, urllib.parse
import numpy as np
from curl_cffi import requests as cr

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR') or os.path.join(HERE, 'history')
CORPUS = os.path.join(HIST, 'esb_bb_games.jsonl')
OUT = os.path.join(HIST, 'esb_bb_edges.jsonl')
sys.path.insert(0, HERE)
import esb_bb_picker as bb  # reuse corpus loader, ridge fit, prob_home

ACC = 'ZTllNjllODUtOWQwNS00YmU4LWE4NTEtZGZjOTkzMGM5OWU4'
HOST = 'www.nj.betmgm.com'
UA = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://betsapi.com/'}

# MGM full team names -> the NBA 2K abbreviations used in the BetsAPI corpus
_TEAM_ALIAS = {
    'los angeles lakers': 'LA Lakers', 'new york knicks': 'NY Knicks',
    'miami heat': 'MIA Heat', 'chicago bulls': 'CHI Bulls',
    'toronto raptors': 'TOR Raptors', 'phoenix suns': 'PHX Suns',
    'cleveland cavaliers': 'CLE Cavaliers', 'denver nuggets': 'DEN Nuggets',
    'orlando magic': 'ORL Magic', 'brooklyn nets': 'BKN Nets',
    'boston celtics': 'BOS Celtics', 'oklahoma city thunder': 'OKC Thunder',
    'memphis grizzlies': 'MEM Grizzlies', 'milwaukee bucks': 'MIL Bucks',
    'dallas mavericks': 'DAL Mavericks', 'charlotte hornets': 'CHA Hornets',
    'golden state warriors': 'GS Warriors', 'utah jazz': 'UTA Jazz',
    'san antonio spurs': 'SA Spurs', 'sacramento kings': 'Sacramento Kings',
    'atlanta hawks': 'Atlanta Hawks', 'portland trail blazers': 'POR Trail Blazers',
    'minnesota timberwolves': 'MIN Timberwolves', 'indiana pacers': 'Indiana Pacers',
    'philadelphia 76ers': 'Philadelphia 76ers', 'new orleans pelicans': 'NO Pelicans',
    'detroit pistons': 'Detroit Pistons', 'washington wizards': 'Washington Wizards',
    'houston rockets': 'Houston Rockets', 'los angeles clippers': 'LA Clippers',
}


def _brand(mgm_name):
    return _TEAM_ALIAS.get((mgm_name or '').strip().lower(), (mgm_name or '').strip())


def mgm_session():
    s = cr.Session(impersonate='safari')
    s.get(f'https://{HOST}/en/sports/esoccer-108', timeout=10)
    return s


def mgm_fixtures(s):
    u = (f'https://{HOST}/cds-api/bettingoffer/fixtures?x-bwin-accessid={ACC}'
         f'&lang=en-us&country=US&userCountry=US&subdivision=US-XY'
         f'&fixtureTypes=Standard,PairGame&state=Latest&offerMapping=Filtered'
         f'&offerCategories=Gridable&fixtureCategories=Gridable,NonGridable,Other'
         f'&skip=0&take=100&sortBy=Tags&sportIds=119')
    return (s.get(u, timeout=15).json() or {}).get('fixtures') or []


def mgm_odds(s, fid):
    fid = urllib.parse.quote(str(fid), safe='')
    u = (f'https://{HOST}/cds-api/bettingoffer/fixture-view?x-bwin-accessid={ACC}'
         f'&lang=en-us&country=US&userCountry=US&subdivision=US-XY&offerMapping=All'
         f'&scoreboardMode=Full&fixtureIds={fid}&state=Latest'
         f'&includePrecreatedBetBuilder=true&supportVirtual=true'
         f'&isBettingInsightsEnabled=true&useRegionalisedConfiguration=true'
         f'&includeRelatedFixtures=false&statisticsModes=All&firstMarketGroupOnly=false')
    d = s.get(u, timeout=15).json() or {}
    f = d.get('fixture') or {}
    out = {'name': (f.get('name') or {}).get('value'), 'start': f.get('startDate'), 'markets': {}}
    for m in f.get('optionMarkets') or []:
        nm = (m.get('name') or {}).get('value') or '?'
        opts = []
        for o in m.get('options') or []:
            p = o.get('price') or {}
            if not p or p.get('americanOdds') is None:
                continue
            opts.append({'name': (o.get('name') or {}).get('value'),
                         'am': p.get('americanOdds')})
        if opts:
            out['markets'][nm] = opts
    return out


def am_to_dec(am):
    return 1 + (am / 100.0 if am > 0 else 100.0 / abs(am))


def main():
    games = bb.load_games()
    if len(games) < 250:
        print('edges: corpus too small'); return
    enc, w, pool = bb.forecast(games)

    s = mgm_session()
    fxs = mgm_fixtures(s)
    now = time.time()
    pat = re.compile(r'^(.*?)\s*\((.+?)\)$')
    # minimum corpus sample per gamer — thin samples inflate extreme probs
    cnt = {}
    for g in games:
        cnt[g['a']] = cnt.get(g['a'], 0) + 1
        cnt[g['b']] = cnt.get(g['b'], 0) + 1
    MIN_GAMES = 25
    rows = []
    for f in fxs:
        start = f.get('startDate')
        try:
            ts = time.mktime(time.strptime(start[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone
        except Exception:
            continue
        if ts < now + 60 or ts > now + 3600 * 3:
            continue
        nm = (f.get('name') or {}).get('value') or ''
        sides = nm.split(' - ')
        if len(sides) != 2:
            continue
        mh = pat.match(sides[0].strip()); ma = pat.match(sides[1].strip())
        if not (mh and ma):
            continue
        ta, ga = _brand(mh.group(1)), mh.group(2)   # MGM home (first)
        tb, gb = _brand(ma.group(1)), ma.group(2)
        # model: p(home=A wins), expected totals via score pairs
        if ga not in enc.amap or gb not in enc.amap or ta not in enc.bmap or tb not in enc.bmap:
            continue
        if cnt.get(ga, 0) < MIN_GAMES or cnt.get(gb, 0) < MIN_GAMES:
            row = {'event': nm, 'start': start, 'skipped': 'thin sample', 'edges': []}
            rows.append(row)
            continue
        mu_a = bb.side_pred(w, enc, ga, gb, ta, tb)
        mu_b = bb.side_pred(w, enc, gb, ga, tb, ta)
        idx = np.arange(0, len(pool), max(1, len(pool) // 600))[:600]
        sa = np.round(mu_a + np.array([pool[j][0] for j in idx]))
        sb = np.round(mu_b + np.array([pool[j][1] for j in idx]))
        marg = sa - sb
        tot = sa + sb
        od = mgm_odds(s, f.get('id'))
        row = {'event': nm, 'start': start, 'fid': f.get('id'),
               'mu': [round(mu_a, 1), round(mu_b, 1)], 'edges': []}
        for mname, opts in od['markets'].items():
            if mname == 'Moneyline':
                for o in opts:
                    if o['name'] == sides[0].strip():
                        p = float(np.mean(marg > 0))
                    elif o['name'] == sides[1].strip():
                        p = float(np.mean(marg < 0))
                    else:
                        continue
                    dec = am_to_dec(o['am'])
                    ev = p * dec - 1
                    row['edges'].append({'mkt': 'ML', 'side': o['name'], 'am': o['am'],
                                         'p': round(p, 4), 'ev': round(ev, 4)})
            m = re.match(r'^([+-]\d+(?:\.\d+)?)$|^-?\d+(?:\.\d+)?$', '')
            if mname == 'Spread':
                for o in opts:
                    h = re.match(r'^(.*?)\s*\(([+-]?\d+(?:\.\d+)?)\)$', o['name'] or '')
                    if not h:
                        continue
                    side_nm, line = h.group(1).strip(), float(h.group(2))
                    # line is the handicap added to that team's score:
                    #   home covers iff marg > -line ; away covers iff marg < line
                    if side_nm == sides[0].strip():
                        p = float(np.mean(marg > -line))
                    elif side_nm == sides[1].strip():
                        p = float(np.mean(marg < line))
                    else:
                        continue
                    dec = am_to_dec(o['am'])
                    ev = p * dec - 1
                    row['edges'].append({'mkt': 'SPREAD', 'side': o['name'], 'am': o['am'],
                                         'p': round(p, 4), 'ev': round(ev, 4)})
            if mname in ('Totals', 'Total Points', 'Total'):
                for o in opts:
                    h = re.match(r'^(O|U)\s*([\d.]+)', o['name'] or '')
                    if not h:
                        continue
                    line = float(h.group(2))
                    p = float(np.mean(tot > line)) if h.group(1) == 'O' else float(np.mean(tot < line))
                    dec = am_to_dec(o['am'])
                    ev = p * dec - 1
                    row['edges'].append({'mkt': 'TOTAL', 'side': o['name'], 'am': o['am'],
                                         'p': round(p, 4), 'ev': round(ev, 4)})
        row['edges'].sort(key=lambda x: -x['ev'])
        rows.append(row)
        if os.path.exists(OUT):
            pass
    with open(OUT, 'a') as fh:
        fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                             'rows': rows}) + '\n')
    # stdout: top edges
    flat = []
    for r in rows:
        for e in r['edges'][:2]:
            flat.append((e['ev'], r['event'], e))
    flat.sort(reverse=True)
    for ev, evm, e in flat[:10]:
        print(f"EV {ev * 100:+6.1f}%  {evm[:44]:<44} {e['mkt']:<7} {e['side'][:28]:<28} @{e['am']}  p={e['p']:.3f}")
    print(f"rows: {len(rows)} (with odds: {sum(1 for r in rows if r['edges'])})")


if __name__ == '__main__':
    main()
