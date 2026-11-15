"""ridge_engine.py — reconstruction of the board's score model per the friend's blueprint
(Ridge_Regression_Modeling_with_Board_Reconstruction.md):

  1. Ridge score model: each NBA-2K game is two team-game rows.
       y = mu + O[alias_i] - D[alias_j] + O_brand[team_i] - D_brand[team_j]
     (players are stable identities who play as varying NBA team brands; BOTH layers
      carry skill — the player's and the roster quality of the brand they used)
  2. Walk-forward fitting: every REFIT_EVERY games, refit on strictly-prior games only;
     forecasts + paired out-of-sample residuals are frozen per game (leakage-safe).
  3. Market evaluators: paired-residual enumeration -> exact spread/total/ML
     win/push/loss probabilities.
  4. Odds/EV: American -> decimal -> EV with pushes.

Only NBA (eBasketball) games are modeled in v1. FIFA needs a draw/discrete-score
layer (blueprint A.15) — later.

Data: history/real_games.json (fixtures + final scores, both aliases and team brands).
"""
import json, os, re, datetime as dt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
GAMES = os.path.join(HERE, 'history', 'real_games.json')

MIN_TRAIN = 400      # games before the first forecast
REFIT_EVERY = 50     # refit cadence (games)


# ---------------------------------------------------------------- data ----
def load_games(nba_only=True):
    """All games as dicts sorted by start. Each: start(dt), a, b (aliases),
    ta, tb (team brands), sa, sb (final scores)."""
    raw = json.load(open(GAMES, encoding='utf-8')).get('by_date', {})
    out = []
    # collect league-tagged team names to classify untagged rows
    nba_teams, fifa_teams = set(), set()
    rows = []
    for day, games in raw.items():
        for g in games:
            try:
                sc = g['score']
                if not sc or sc == [0, 0] or g.get('status') != 'MATCH_ENDED':
                    continue
                start = dt.datetime.strptime(g['start'][:19], '%Y-%m-%dT%H:%M:%S'
                                              ).replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            r = {'start': start, 'a': g['aliases'][0].strip().upper(),
                 'b': g['aliases'][1].strip().upper(),
                 'ta': (g.get('teams') or ['?', '?'])[0], 'tb': (g.get('teams') or ['?', '?'])[1],
                 'sa': float(sc[0]), 'sb': float(sc[1]), 'league': g.get('league')}
            rows.append(r)
            if r['league'] == 'nba':
                nba_teams.update([r['ta'], r['tb']])
            elif r['league'] == 'fifa':
                fifa_teams.update([r['ta'], r['tb']])
    for r in rows:
        if r['league'] is None:
            r['league'] = 'nba' if (r['ta'] in nba_teams and r['tb'] in nba_teams) else (
                          'fifa' if (r['ta'] in fifa_teams and r['tb'] in fifa_teams) else 'other')
        if nba_only and r['league'] != 'nba':
            continue
        out.append(r)
    out.sort(key=lambda r: r['start'])
    return out


# ---------------------------------------------------------------- fit ----
class Encoder:
    """Maps aliases and team brands to coefficient indices.
    Layout: [0]=intercept, then O/D for aliases, then O/D for team brands."""

    def __init__(self, games):
        self.aliases = sorted({r['a'] for r in games} | {r['b'] for r in games})
        self.brands = sorted({r['ta'] for r in games} | {r['tb'] for r in games})
        self.a_idx = {x: 1 + i for i, x in enumerate(self.aliases)}
        n = len(self.aliases)
        self.d_idx = {x: 1 + n + i for i, x in enumerate(self.aliases)}
        self.bO_idx = {x: 1 + 2 * n + i for i, x in enumerate(self.brands)}
        m = len(self.brands)
        self.bD_idx = {x: 1 + 2 * n + m + i for i, x in enumerate(self.brands)}
        self.dim = 1 + 2 * n + 2 * m
        self.n_alias = n

    def rows_for_game(self, g):
        """Two rows (X, y): score of a vs b's defense, and vice versa."""
        X = np.zeros((2, self.dim))
        X[:, 0] = 1.0                     # intercept
        y = np.array([g['sa'], g['sb']])
        X[0, self.a_idx[g['a']]] = 1.0
        X[0, self.d_idx[g['b']]] = -1.0
        X[0, self.bO_idx[g['ta']]] = 1.0
        X[0, self.bD_idx[g['tb']]] = -1.0
        X[1, self.a_idx[g['b']]] = 1.0
        X[1, self.d_idx[g['a']]] = -1.0
        X[1, self.bO_idx[g['tb']]] = 1.0
        X[1, self.bD_idx[g['ta']]] = -1.0
        return X, y


def fit_ridge(enc, games, lam=30.0, weights=None):
    """Ridge fit; intercept unpenalized, all team/player effects penalized.
    weights: optional per-game recency weights (same weight for both rows)."""
    Xs, ys = [], []
    for i, g in enumerate(games):
        X, y = enc.rows_for_game(g)
        w = 1.0 if weights is None else weights[i]
        Xs.append(X * w); ys.append(y * w)
    X = np.vstack(Xs); y = np.concatenate(ys)
    P = np.ones(enc.dim)
    P[0] = 0.0                        # intercept unpenalized
    A = X.T @ X + lam * np.diag(P)
    rhs = X.T @ y
    return np.linalg.solve(A, rhs)


def predict(w, enc, g):
    """Expected scores (sa_hat, sb_hat) for a game."""
    X, _ = enc.rows_for_game(g)
    return X @ w


# ------------------------------------------------- walk-forward engine ----
def walk_forward(games, min_train=MIN_TRAIN, refit_every=REFIT_EVERY, lam=30.0,
                 halflife=None):
    """Frozen, leakage-safe forecasts + paired residuals for every game after
    the warmup. halflife: if set, games are recency-weighted with exponential
    decay (half-life in games) at each refit (blueprint A.15)."""
    import math
    enc = Encoder(games)
    out = []
    w = None
    for i, g in enumerate(games):
        if i >= min_train and (w is None or (i - min_train) % refit_every == 0):
            if halflife:
                ages = np.arange(i, 0, -1, dtype=float)
                weights = np.power(0.5, ages / halflife)
                weights = np.concatenate([[1.0], weights])  # align to games[:i]
                w = fit_ridge(enc, games[:i], lam, weights=weights[:i])
            else:
                w = fit_ridge(enc, games[:i], lam)
        if w is None:
            continue
        sa_h, sb_h = predict(w, enc, g)
        out.append({**g, 'idx': i, 'sa_hat': sa_h, 'sb_hat': sb_h,
                    'eA': g['sa'] - sa_h, 'eB': g['sb'] - sb_h})
    return out


# ------------------------------------------------------- market probs ----
def market_probs(fc, pool, max_pool=600):
    """Exact enumeration of paired residuals -> market probabilities.
    fc: forecast dict (sa_hat, sb_hat, a, b). pool: list of (eA, eB) residual
    pairs from strictly earlier games (already out-of-sample).
    Returns dict of prob helpers."""
    pool = pool[-max_pool:]
    if not pool:
        return None
    A = fc['sa_hat'] + np.array([p[0] for p in pool])
    B = fc['sb_hat'] + np.array([p[1] for p in pool])
    n = float(len(pool))
    return {
        'total_over': lambda line: (np.mean(A + B > line), np.mean(A + B == line)),
        'spread_cover': lambda side_alias, L: (
            np.mean((A if side_alias == fc['a'] else B) + L > (B if side_alias == fc['a'] else A)),
            np.mean((A if side_alias == fc['a'] else B) + L == (B if side_alias == fc['a'] else A))),
        'ml_win': lambda side_alias: np.mean(
            (A > B) if side_alias == fc['a'] else (B > A)),
        'n': int(n),
    }


def parse_side(side):
    """'Over 113.5' / 'Under 119.5' / 'BLADE +4.5' / 'NIGHTHAWK -2.5' / 'X to win'
    -> (market, params)."""
    s = (side or '').strip()
    m = re.match(r'(?i)^(Over|Under)\s+([\d.]+)$', s)
    if m:
        return 'total', (m.group(1).lower(), float(m.group(2)))
    m = re.match(r'(?i)^([A-Za-z0-9 ]+?)\s+([+-])([\d.]+)$', s)
    if m:
        return 'spread', (m.group(1).strip().upper(), float(m.group(2) + m.group(3)))
    m = re.match(r'(?i)^([A-Za-z0-9 ]+?)\s+to win$', s)
    if m:
        return 'ml', (m.group(1).strip().upper(),)
    return None, None


def prob_for_pick(probs, side):
    """(p_win, p_push) for a pick string, using the enumeration helpers."""
    mk, p = parse_side(side)
    if mk == 'total':
        ov, line = p
        w, push = probs['total_over'](line)
        return (w if ov == 'over' else 1 - w - push, push)
    if mk == 'spread':
        alias, L = p
        w, push = probs['spread_cover'](alias, L)
        return (w, push)
    if mk == 'ml':
        alias = p[0]
        w = probs['ml_win'](alias)
        return (w, 0.0)
    return None, None


def parlay_probs(game_forecasts, legs, pool_per_game, max_pool=600):
    """Joint parlay probability via scenario-index enumeration (blueprint A.8).

    game_forecasts: list of forecast dicts, one per distinct game in the parlay
                    (order matches legs' game index)
    legs: list of (game_idx, side) e.g. [(0, 'Over 105.5'), (0, 'NIGHTHAWK -2.5'), (1, 'Under 99.5')]
    pool_per_game: list of residual-pool lists, one per game (each strictly past-only)
    Same-game legs share the SAME scenario draw -> preserves within-game dependence.
    Cross-game legs are combined by scenario index (independence across games is the
    baseline; the pool draw is separate per game).

    Returns (p_win, p_any_push); pushing legs void (standard parlay rules).
    """
    import random as _random
    N = 20000
    rng = _random.Random(1234)
    alive = np.ones(N, dtype=bool)
    pushed = np.zeros(N, dtype=bool)
    scen = {}
    for gi, (fc, pool) in enumerate(zip(game_forecasts, pool_per_game)):
        pool = pool[-max_pool:]
        if not pool:
            return None
        # independent scenario draw PER GAME (seeded): same-game legs share these
        # draws; cross-game legs are independent by construction
        idx = [rng.randrange(len(pool)) for _ in range(N)]
        eA = np.array([pool[i][0] for i in idx])
        eB = np.array([pool[i][1] for i in idx])
        scen[gi] = (fc['sa_hat'] + eA, fc['sb_hat'] + eB)

    for gi, side in legs:
        A, B = scen[gi]
        fc = game_forecasts[gi]
        mk, p = parse_side(side)
        if mk == 'total':
            tot = A + B; line = p[1]
            win = (tot > line) if p[0] == 'over' else (tot < line)
            push = (tot == line)
        elif mk == 'spread':
            alias, L = p
            own = A if alias == fc['a'] else B
            opp = B if alias == fc['a'] else A
            marg = own + L - opp
            win = marg > 0
            push = marg == 0
        elif mk == 'ml':
            alias = p[0]
            own = A if alias == fc['a'] else B
            opp = B if alias == fc['a'] else A
            win = own > opp
            push = own == opp
        else:
            return None
        # standard parlay settlement: pushing leg is removed (no loss), losing leg kills ticket
        alive &= ~push
        alive &= win
        pushed |= push
    return (float(np.mean(alive)), float(np.mean(pushed & alive)))


# ------------------------------------------------------------- odds/EV ----
def american_to_decimal(a):
    a = float(a)
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / abs(a)


def ev_from_prob(p_win, p_push, american):
    """Net EV per $1 staked, pushes return stake."""
    d = american_to_decimal(american)
    p_loss = 1.0 - p_win - p_push
    return p_win * (d - 1.0) - p_loss
