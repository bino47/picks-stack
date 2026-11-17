#!/usr/bin/env python
"""esb_bb_picker.py — eBasketball GG 4x5 (league 25067) EV paper picks.

INTAKE (fixed 2026-11-17): BetsAPI's /events/upcoming feed never lists league
25067 fixtures in advance (verified: league-scoped upcoming returns 0; the
day-scoped feed only carries stale, already-started rows), so the old
upcoming-based pick() found nothing forever. Picks are now taken from the
latest MGM scan in history/esb_bb_edges.jsonl (written every cycle by
esb_bb_edges.py): per-gamer ridge p_model vs REAL MGM americanOdds, with the
edges script's own guards (>=25 games per gamer, 2min-3h start window).

  python esb_bb_picker.py pick      # new paper picks from the latest edges scan
  python esb_bb_picker.py settle    # grade matured picks vs BetsAPI ended events
  python esb_bb_picker.py report    # ledger summary

Ledger row (history/esb_bb_paper_ledger.jsonl): one pick per fixture ever
(dedupe by event+start); real MGM price recorded at pick time; quarter-Kelly
stake clamped to [MIN_STAKE, 2.5% bankroll]; longshot cap MAX_DEC. Settlement
matches BetsAPI ended events by unordered gamer-tag pair + start time within
+-10 min (the same gamer pair rematches constantly, so time is the
disambiguator). Unmatched picks older than 30h expire (mirrors the esoccer
expired_unmatched rule).
"""
import json, os, re, sys, time, urllib.request
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR') or os.path.join(HERE, 'history')
CORPUS = os.path.join(HIST, 'esb_bb_games.jsonl')
LEDGER = os.path.join(HIST, 'esb_bb_paper_ledger.jsonl')
EDGES = os.path.join(HIST, 'esb_bb_edges.jsonl')
TOK = (os.environ.get('BETSAPI_KEY') or '').strip()
UA = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://betsapi.com/'}
LEAGUE = 25067
MIN_TRAIN = 200
REFIT_EVERY = 100
LAM = 8.0
EV_BAR = 0.05          # min EV to log a pick
MAX_DEC = 4.0          # longshot cap: skip anything worse than +300
KELLY_FRAC = 0.25
MIN_STAKE = 5.0
MAX_STAKE_PCT = 0.025
BANK0 = 1000.0
MATCH_WINDOW_S = 600   # start-time tolerance when matching ended events
EXPIRE_AFTER_S = 30 * 3600

TAG = re.compile(r'^(.*?)\s*\((.+?)\)$')


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


def load_games():
    pat = re.compile(r'^(.*?)\s*\((.+?)\)$')
    games = []
    for l in open(CORPUS, encoding='utf-8'):
        r = json.loads(l)
        m = pat.match(r['home'] or ''); n = pat.match(r['away'] or '')
        if not (m and n):
            continue
        games.append({'a': m.group(2), 'b': n.group(2), 'ta': m.group(1), 'tb': n.group(1),
                      'sa': r['home_score'], 'sb': r['away_score'], 'time': r['time'], 'id': r['id']})
    games.sort(key=lambda g: (g['time'], g['id']))
    return games


class Enc:
    def __init__(self, gs):
        aliases = {g['a'] for g in gs} | {g['b'] for g in gs}
        brands = {g['ta'] for g in gs} | {g['tb'] for g in gs}
        self.amap = {p: i for i, p in enumerate(sorted(aliases))}
        self.bmap = {t: i for i, t in enumerate(sorted(brands))}
        self.nA, self.nB = len(self.amap), len(self.bmap)

    def rows(self, g):
        n = 1 + 2 * self.nA + 2 * self.nB
        rh = np.zeros(n); ra = np.zeros(n)
        rh[0] = ra[0] = 1.0
        rh[1 + 2 * self.amap[g['a']]] = 1.0; rh[1 + self.nA + self.amap[g['b']]] = -1.0
        rh[1 + 2 * self.nA + 2 * self.bmap[g['ta']]] = 1.0
        rh[1 + 2 * self.nA + self.nB + self.bmap[g['tb']]] = -1.0
        ra[1 + 2 * self.amap[g['b']]] = 1.0; ra[1 + self.nA + self.amap[g['a']]] = -1.0
        ra[1 + 2 * self.nA + 2 * self.bmap[g['tb']]] = 1.0
        ra[1 + 2 * self.nA + self.nB + self.bmap[g['ta']]] = -1.0
        return rh, ra


def fit(enc, gs, lam=LAM):
    X, y = [], []
    for g in gs:
        rh, ra = enc.rows(g); X += [rh, ra]; y += [g['sa'], g['sb']]
    X = np.array(X); y = np.array(y, float)
    A = X.T @ X + lam * np.eye(X.shape[1]); A[0, 0] -= lam
    return np.linalg.solve(A, X.T @ y)


def side_pred(w, enc, own, opp, t_own, t_opp):
    r = np.zeros(1 + 2 * enc.nA + 2 * enc.nB); r[0] = 1.0
    r[1 + 2 * enc.amap[own]] = 1.0; r[1 + enc.nA + enc.amap[opp]] = -1.0
    r[1 + 2 * enc.nA + 2 * enc.bmap[t_own]] = 1.0
    r[1 + 2 * enc.nA + enc.nB + enc.bmap[t_opp]] = -1.0
    return float(r @ w)


def forecast(games):
    enc = Enc(games)
    w = fit(enc, games)
    pool = [(g['sa'] - side_pred(w, enc, g['a'], g['b'], g['ta'], g['tb']),
             g['sb'] - side_pred(w, enc, g['b'], g['a'], g['tb'], g['ta'])) for g in games]
    return enc, w, pool


def prob_home(enc, w, pool, a, b, ta, tb):
    if a not in enc.amap or b not in enc.amap or ta not in enc.bmap or tb not in enc.bmap:
        return None
    mu_a = side_pred(w, enc, a, b, ta, tb)
    mu_b = side_pred(w, enc, b, a, tb, ta)
    idx = np.arange(0, len(pool), max(1, len(pool) // 600))[:600]
    sa = np.round(mu_a + np.array([pool[j][0] for j in idx]))
    sb = np.round(mu_b + np.array([pool[j][1] for j in idx]))
    return float(np.mean(sa > sb)), float(np.mean(sa + sb)), (mu_a, mu_b)


def load_ledger():
    if not os.path.exists(LEDGER):
        return []
    return [json.loads(l) for l in open(LEDGER, encoding='utf-8') if l.strip()]


def bankroll(ledger):
    return BANK0 + sum(o.get('pnl', 0) for o in ledger if o.get('status') == 'settled')


def am_to_dec(am):
    return 1 + (am / 100.0 if am > 0 else 100.0 / abs(am))


def latest_scan():
    if not os.path.exists(EDGES):
        return None
    last = ''
    for l in open(EDGES, encoding='utf-8'):
        if l.strip():
            last = l
    if not last:
        return None
    try:
        return json.loads(last)
    except Exception:
        return None


def _tags(name):
    m = TAG.match((name or '').strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else (None, None)


def pick():
    scan = latest_scan()
    if not scan or not scan.get('rows'):
        print('bb pick: no edges scan available')
        return
    ledger = load_ledger()
    # one pick per fixture EVER (event+start); the tag pair rematches all day,
    # so (event, start) is the fixture identity across scans
    known = {(o.get('event'), o.get('start')) for o in ledger}
    bank = bankroll(ledger)
    new = 0
    for r in scan['rows']:
        if r.get('skipped') or not r.get('edges'):
            continue
        sides = (r.get('event') or '').split(' - ')
        if len(sides) != 2:
            continue
        try:
            st = time.mktime(time.strptime((r['start'] or '')[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone
        except Exception:
            continue
        if (r['event'], r['start']) in known:
            continue
        best = None
        for e in r['edges']:
            if (e.get('ev') or 0) < EV_BAR:
                continue
            dec = am_to_dec(e.get('am') or 0)
            if not dec or dec < 1.01 or dec > MAX_DEC:
                continue        # longshot cap / malformed price
            if best is None or e['ev'] > best[0]['ev']:
                best = (e, dec)
        if not best:
            continue
        e, dec = best
        # parse the MGM option name into a gradable structure
        row = None
        if e['mkt'] == 'ML':
            team, tag = _tags(e['side'])
            if not tag:
                continue
            row = {'market': 'ML', 'side': e['side'], 'side_tag': tag, 'line': None, 'ou': None}
        elif e['mkt'] == 'SPREAD':
            m = re.match(r'^(.*)\s+\(([+-]?\d+(?:\.\d+)?)\)$', (e['side'] or '').strip())
            if not m:
                continue
            team, tag = _tags(m.group(1))
            if not tag:
                continue
            row = {'market': 'SPREAD', 'side': e['side'], 'side_tag': tag,
                   'line': float(m.group(2)), 'ou': None}
        elif e['mkt'] in ('TOTAL', 'Total'):
            m = re.match(r'^([OU])\s*([\d.]+)$', (e['side'] or '').strip())
            if not m:
                continue
            row = {'market': 'TOTAL', 'side': '%s %s pts' % (
                       'Over' if m.group(1) == 'O' else 'Under', m.group(2)),
                   'side_tag': None, 'line': float(m.group(2)), 'ou': m.group(1)}
        else:
            continue
        p = float(e['p'])
        f = 0.25 * max(0.0, (p * dec - 1) / (dec - 1))
        stake = round(min(MAX_STAKE_PCT * bank, max(MIN_STAKE, f * bank)), 2)
        ta = _tags(sides[0]); tb = _tags(sides[1])
        row.update({
            'bbid': '%d_%s_%s' % (int(st), (ta[1] or 'x').lower(), (tb[1] or 'y').lower()),
            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'event': r['event'], 'start': r['start'], 'start_ts': int(st),
            'home': sides[0].strip(), 'away': sides[1].strip(),
            'tag_a': ta[1], 'tag_b': tb[1],
            'price_am': e['am'], 'dec_odds': round(dec, 3),
            'p_model': round(p, 4), 'ev': round(float(e['ev']), 4),
            'stake': stake, 'status': 'open',
            'fid': r.get('fid'), 'mu': r.get('mu'), 'book': 'betmgm',
        })
        ledger.append(row)
        known.add((r['event'], r['start']))
        new += 1
        print("NEW bb | %s | %s %s @%s | p=%.3f EV=%+.1f%% | stake $%.2f"
              % (r['event'], row['market'], row['side'], row['price_am'],
                 p, 100 * row['ev'], stake))
    if new:
        with open(LEDGER, 'w', encoding='utf-8') as f:
            for o in ledger:
                f.write(json.dumps(o) + '\n')
    print('bb new picks: %d (scan rows: %d)' % (new, len(scan['rows'])))


def fetch_ended(depth_pages=4):
    """newest ended league-25067 events, for settlement matching"""
    out = []
    for page in range(1, depth_pages + 1):
        d = get('https://api.betsapi.com/v1/events/ended?token=%s&sport_id=18&league_id=%s&page=%d'
                % (TOK, LEAGUE, page))
        rs = d.get('results') or []
        if not rs:
            break
        out += rs
        time.sleep(0.5)
    return out


def _grade(o, ev):
    """win/loss/push for an open pick vs a matched BetsAPI ended event"""
    hm, aw = (ev.get('home') or {}).get('name') or '', (ev.get('away') or {}).get('name') or ''
    if not hm or not aw or not ev.get('ss') or '-' not in ev['ss']:
        return None
    try:
        hs, as_ = (int(x) for x in ev['ss'].split('-'))
    except Exception:
        return None
    ht, at = _tags(hm)[1], _tags(aw)[1]
    if not ht or not at:
        return None
    mkt = o.get('market')
    if mkt == 'ML':
        if hs == as_:
            return 'push'
        home_won = hs > as_
        return 'win' if (o['side_tag'] == ht) == home_won else 'loss'
    if mkt == 'SPREAD':
        if o['side_tag'] == ht:
            adj = (hs - as_) + float(o['line'])
        elif o['side_tag'] == at:
            adj = (as_ - hs) + float(o['line'])
        else:
            return None
        return 'win' if adj > 1e-9 else ('push' if abs(adj) < 1e-9 else 'loss')
    if mkt == 'TOTAL':
        tot = hs + as_
        if tot == float(o['line']):
            return 'push'
        over = tot > float(o['line'])
        return 'win' if over == (o['ou'] == 'O') else 'loss'
    return None


def settle():
    ledger = load_ledger()
    opens = [o for o in ledger if o.get('status') == 'open']
    if not opens:
        print('bb: nothing to settle')
        return
    ended = fetch_ended()
    now = time.time()
    settled = expired = 0
    for o in opens:
        want = {o.get('tag_a'), o.get('tag_b')}
        best, bd = None, None
        for ev in ended:
            hm, aw = (ev.get('home') or {}).get('name'), (ev.get('away') or {}).get('name')
            if {_tags(hm)[1], _tags(aw)[1]} != want:
                continue
            d = abs(int(ev.get('time') or 0) - o.get('start_ts', 0))
            if d <= MATCH_WINDOW_S and (bd is None or d < bd):
                best, bd = ev, d
        if best is not None:
            res = _grade(o, best)
            if res:
                o['status'] = 'settled'
                o['result'] = res
                try:
                    hs, as_ = (int(x) for x in best['ss'].split('-'))
                    o['final'] = '%d-%d' % (hs, as_)
                except Exception:
                    pass
                o['pnl'] = (round(o['stake'] * (o['dec_odds'] - 1), 2) if res == 'win'
                            else (-o['stake'] if res == 'loss' else 0))
                settled += 1
                continue
        if now - o.get('start_ts', 0) > EXPIRE_AFTER_S:
            o['status'] = 'expired'
            o['pnl'] = 0
            expired += 1
    with open(LEDGER, 'w', encoding='utf-8') as f:
        for o in ledger:
            f.write(json.dumps(o) + '\n')
    print('bb settled %d, expired %d (ended pool %d)' % (settled, expired, len(ended)))


def report():
    ledger = load_ledger()
    s = [o for o in ledger if o.get('status') == 'settled']
    n = len(s); wins = sum(1 for o in s if o['result'] == 'win')
    pushes = sum(1 for o in s if o['result'] == 'push')
    pnl = sum(o.get('pnl', 0) for o in s)
    staked = sum(o['stake'] for o in s)
    print('bb ledger: %d settled %dW-%dL-%dP  ROI=%+.1f%%  pnl=$%.2f  open=%d'
          % (n, wins, n - wins - pushes, pushes,
             (pnl / staked * 100 if staked else 0), pnl,
             sum(1 for o in ledger if o.get('status') == 'open')))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'pick'
    if cmd == 'pick':
        pick()
    elif cmd == 'settle':
        settle()
    elif cmd == 'report':
        report()
    else:
        print('usage: esb_bb_picker.py pick|settle|report')


if __name__ == '__main__':
    main()
