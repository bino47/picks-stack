#!/usr/bin/env python
"""esb_model.py — ridge score model for the EsportsBattle soccer leagues.

Mirrors ridge_engine.py's math, adapted to low-scoring soccer:
  y_goals(side) = mu + O[player_alias] - D[opp_alias]
                      + O_brand[team]   - D_brand[opp_team]
  per league (battle8 / volta6 have different scoring paces).

Forecast: per-side expected goals + the walk-forward PAIRED residual pool ->
discrete score-pair distribution (rounded residuals) -> exact
P(home win / draw / away win) and total Over/Under (with push).

Commands:
  python esb_model.py calibrate      # walk-forward calibration vs corpus
  python esb_model.py pair LEAGUE PLAYER_A PLAYER_B   # one forecast, current fit
"""
import json, os, re, sys, collections
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(os.environ.get('SCORER_HISTORY_DIR', os.path.dirname(os.path.abspath(__file__))), 'esb_games.jsonl')
MIN_TRAIN = 300
REFIT_EVERY = 50
LAM = 8.0
MAX_POOL = 600


def load_games():
    rows = [json.loads(l) for l in open(CORPUS, encoding='utf-8')]
    games = []
    for r in rows:
        m = re.match(r'^(.*?)\s*\((.+?)\)$', (r.get('home') or '').strip())
        n = re.match(r'^(.*?)\s*\((.+?)\)$', (r.get('away') or '').strip())
        if not (m and n and r.get('home_alias') and r.get('away_alias')):
            continue
        games.append({'league': r['league'], 'time': r.get('time') or 0,
                      'a': r['home_alias'], 'b': r['away_alias'],
                      'ta': m.group(1), 'tb': n.group(1),
                      'sa': int(r['home_score']), 'sb': int(r['away_score']),
                      'id': r['id']})
    games.sort(key=lambda g: (g['league'], int(g['time']), g['id']))
    return games


class Enc:
    """Feature encoder: two rows per game (home side, away side)."""

    def __init__(self, games):
        aliases, brands = set(), set()
        for g in games:
            aliases |= {g['a'], g['b']}
            brands |= {g['ta'], g['tb']}
        self.amap = {p: i for i, p in enumerate(sorted(aliases))}
        self.bmap = {t: i for i, t in enumerate(sorted(brands))}
        self.nA, self.nB = len(self.amap), len(self.bmap)

    def rows_for_game(self, g):
        # columns: [mu, O_a, D_b, O_ta, D_tb] for the home side, mirrored for away
        n = 1 + 2 * self.nA + 2 * self.nB
        rh = np.zeros(n); ra = np.zeros(n)
        rh[0] = 1.0; ra[0] = 1.0
        rh[1 + 2 * self.amap[g['a']]] = 1.0            # O home player
        rh[1 + self.nA + self.amap[g['b']]] = -1.0     # D away player
        rh[1 + 2 * self.nA + 2 * self.bmap[g['ta']]] = 1.0
        rh[1 + 2 * self.nA + self.nB + self.bmap[g['tb']]] = -1.0
        ra[1 + 2 * self.amap[g['b']]] = 1.0
        ra[1 + self.nA + self.amap[g['a']]] = -1.0
        ra[1 + 2 * self.nA + 2 * self.bmap[g['tb']]] = 1.0
        ra[1 + 2 * self.nA + self.nB + self.bmap[g['ta']]] = -1.0
        return rh, ra


def fit_ridge(enc, games, lam=LAM):
    X, y = [], []
    for g in games:
        rh, ra = enc.rows_for_game(g)
        X.append(rh); y.append(g['sa'])
        X.append(ra); y.append(g['sb'])
    X = np.array(X); y = np.array(y, dtype=float)
    A = X.T @ X + lam * np.eye(X.shape[1])
    A[0, 0] -= lam                       # don't penalize the intercept
    w = np.linalg.solve(A, X.T @ y)
    return w


def predict_side(w, enc, league_home, opp, team, opp_team):
    r = np.zeros(1 + 2 * enc.nA + 2 * enc.nB)
    r[0] = 1.0
    r[1 + 2 * enc.amap[league_home]] = 1.0
    r[1 + enc.nA + enc.amap[opp]] = -1.0
    r[1 + 2 * enc.nA + 2 * enc.bmap[team]] = 1.0
    r[1 + 2 * enc.nA + enc.nB + enc.bmap[opp_team]] = -1.0
    return float(r @ w)


def forecast(w, enc, g, pool):
    """pool = list of (res_home, res_away) from strictly-prior games.
    Returns None when a player or team brand has no history (new entrant)."""
    for p in (g['a'], g['b']):
        if p not in enc.amap:
            return None
    for t in (g['ta'], g['tb']):
        if t not in enc.bmap:
            return None
    mu_a = predict_side(w, enc, g['a'], g['b'], g['ta'], g['tb'])
    mu_b = predict_side(w, enc, g['b'], g['a'], g['tb'], g['ta'])
    if not pool:
        return None
    idx = np.arange(0, len(pool), max(1, len(pool) // MAX_POOL))[:MAX_POOL]
    ra = np.array([pool[i][0] for i in idx])
    rb = np.array([pool[i][1] for i in idx])
    sa = np.clip(np.round(mu_a + ra), 0, 30).astype(int)
    sb = np.clip(np.round(mu_b + rb), 0, 30).astype(int)
    return mu_a, mu_b, sa, sb


def probs_from_scores(sa, sb):
    n = len(sa)
    return {'home': float(np.mean(sa > sb)), 'draw': float(np.mean(sa == sb)),
            'away': float(np.mean(sb > sa)),
            'total': lambda line: (float(np.mean(sa + sb > line)),
                                   float(np.mean(sa + sb == line)))}


def calibrate():
    games = load_games()
    bylg = collections.defaultdict(list)
    for g in games:
        bylg[g['league']].append(g)
    report = {}
    for lg, gs in sorted(bylg.items()):
        enc = Enc(gs)
        w = None
        pool = []
        next_fit = 0
        rows = []
        for i, g in enumerate(gs):
            if i < MIN_TRAIN:
                continue
            if i >= next_fit:
                w = fit_ridge(enc, gs[:i])
                next_fit = i + REFIT_EVERY
            mu_a = predict_side(w, enc, g['a'], g['b'], g['ta'], g['tb'])
            mu_b = predict_side(w, enc, g['b'], g['a'], g['tb'], g['ta'])
            pool.append((g['sa'] - mu_a, g['sb'] - mu_b))
            fc = forecast(w, enc, g, pool[:-1])
            if fc is None:
                continue
            p = probs_from_scores(fc[2], fc[3])
            rows.append({'p_home': p['home'], 'p_draw': p['draw'], 'p_away': p['away'],
                         'home': g['sa'] > g['sb'], 'draw': g['sa'] == g['sb']})
        n = len(rows)
        if not n:
            report[lg] = {'games': len(gs), 'forecastable': 0}
            continue
        # calibration buckets on P(home)
        buckets = [(0, .3), (.3, .45), (.45, .55), (.55, .7), (.7, 1.01)]
        cal = []
        for lo, hi in buckets:
            grp = [r for r in rows if lo <= r['p_home'] < hi]
            if grp:
                cal.append({'p_range': [lo, hi], 'n': len(grp),
                            'avg_p': round(sum(r['p_home'] for r in grp) / len(grp), 3),
                            'realized': round(sum(1 for r in grp if r['home']) / len(grp), 3)})
        base = sum(1 for r in rows if r['home']) / n
        # Brier multioutcome
        brier = sum((r['p_home'] - (1 if r['home'] else 0)) ** 2
                    + (r['p_draw'] - (1 if r['draw'] else 0)) ** 2
                    + (r['p_away'] - (1 if not (r['home'] or r['draw']) else 0)) ** 2
                    for r in rows) / n
        draw_rate = sum(1 for r in rows if r['draw']) / n
        report[lg] = {'games': len(gs), 'forecastable': n,
                      'home_rate': round(base, 3), 'draw_rate': round(draw_rate, 3),
                      'brier': round(brier, 4),
                      'p_home_calibration': cal}
    print(json.dumps(report, indent=1))


def pair(league, a, b, ta='?', tb='?'):
    games = [g for g in load_games() if g['league'] == league]
    enc = Enc(games)
    w = fit_ridge(enc, games)
    pool = [(g['sa'] - predict_side(w, enc, g['a'], g['b'], g['ta'], g['tb']),
             g['sb'] - predict_side(w, enc, g['b'], g['a'], g['tb'], g['ta'])) for g in games]
    g = {'a': a.upper(), 'b': b.upper(), 'ta': ta, 'tb': tb}
    fc = forecast(w, enc, g, pool)
    mu_a, mu_b, sa, sb = fc
    p = probs_from_scores(sa, sb)
    print(json.dumps({'league': league, 'pair': [g['a'], g['b']],
                      'xg': [round(mu_a, 2), round(mu_b, 2)],
                      'p_home': round(p['home'], 3), 'p_draw': round(p['draw'], 3),
                      'p_away': round(p['away'], 3)}, indent=1))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'calibrate'
    if cmd == 'calibrate':
        calibrate()
    elif cmd == 'pair' and len(sys.argv) >= 4:
        pair(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        calibrate()
