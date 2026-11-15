"""model_paper_trader.py — paper-trades MY model's own picks (not the user's placements).

The model (history/source_model.json, trained daily by the learning loop) scores every
board suggestion; history/scored_card.json carries the live card with model_win %, EV and
a bet yes/no flag. This trader takes the picks the MODEL decides to bet, sizes them with
its own quarter-Kelly logic (model_win vs book-implied price, capped), and settles them
against the h2hggl feed with Kalshi contract economics.

Ledger : kalshi-bot/model_ledger.jsonl   State: model_paper_state.json
Run    : python model_paper_trader.py   (add new model picks + settle matured ones)

Only NBA singles are traded — FIFA suggestions can't be settled yet (no results feed),
and the model's own record shows that's where its calibration is untested.
"""
import json, re, sys, os, datetime as dt, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_econ import implied_price, fee_per_contract, kelly_fraction

A = os.environ.get('SCORER_HISTORY_DIR', os.path.dirname(os.path.abspath(__file__)))
CARD = A + '/history/scored_card.json'
FEED = A + '/history/real_games.json'
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, 'model_ledger.jsonl')
STATE = os.path.join(HERE, 'model_paper_state.json')

# ---- my sizing config ----
BANKROLL_START = 1000.0
KELLY_FRACTION = 0.25          # quarter-Kelly on the model's edge
MAX_BET_FRAC = 0.02            # never more than 2% of current paper bankroll
MIN_STAKE = 5.0
SETTLE_AFTER_MIN = 180
TOL = dt.timedelta(minutes=10)


def load_ledger():
    if not os.path.exists(LEDGER):
        return []
    return [json.loads(l) for l in open(LEDGER, encoding='utf-8') if l.strip()]


def parse_pair(m):
    mm = re.match(r'([A-Za-z0-9 ]+?)\s+v\s+([A-Za-z0-9 ]+)', m or '')
    return tuple(sorted((mm.group(1).strip().upper(), mm.group(2).strip().upper()))) if mm else None


def grade(pick, pts):
    s = (pick or '').strip()
    m = re.match(r'(?i)^(Over|Under)\s+([\d.]+)$', s)
    if m:
        tot = sum(pts.values()); line = float(m.group(2))
        if abs(tot - line) < 1e-9: return 'push'
        return 'win' if (tot > line) == (m.group(1).lower() == 'over') else 'loss'
    m = re.match(r'(?i)^([A-Za-z0-9 ]+?)\s+([+-])([\d.]+)$', s)
    if m:
        alias = m.group(1).strip().upper()
        if alias not in pts: return None
        other = [p for a, p in pts.items() if a != alias][0]
        marg = pts[alias] - other; line = float(m.group(3))
        marg = marg + line if m.group(2) == '+' else marg - line
        if abs(marg) < 1e-9: return 'push'
        return 'win' if marg > 0 else 'loss'
    m = re.match(r'(?i)^([A-Za-z0-9 ]+?)\s+to win$', s)
    if m:
        alias = m.group(1).strip().upper()
        if alias not in pts: return None
        other = [p for a, p in pts.items() if a != alias][0]
        if pts[alias] == other: return 'push'
        return 'win' if pts[alias] > other else 'loss'
    return None


def build_feed_index():
    raw = json.load(open(FEED, encoding='utf-8')).get('by_date', {})
    index = collections.defaultdict(list)
    for day, games in raw.items():
        for g in games:
            try:
                sc = g['score']
                if not sc or sc == [0, 0] or g.get('status') != 'MATCH_ENDED':
                    continue
                st = dt.datetime.strptime(g['start'][:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            a, b = g['aliases'][0].strip().upper(), g['aliases'][1].strip().upper()
            index[tuple(sorted((a, b)))].append(
                {'start': st, 'pts': {a: sc[0], b: sc[1]}, 'score': sc, 'teams': g.get('teams')})
    return index


def find_game(index, pair, start):
    cands = index.get(pair)
    if not cands: return None
    best, bd = None, None
    for g in cands:
        d = abs(g['start'] - start)
        if d <= TOL and (bd is None or d < bd):
            best, bd = g, d
    return best


def main(report_only=False):
    now = dt.datetime.now(dt.timezone.utc)
    ledger = load_ledger()
    known = {o['key'] for o in ledger}

    new_orders, settled_now = [], []
    if not report_only:
        # ---- 1) take the model's own picks off the live card ----
        try:
            card = json.load(open(CARD, encoding='utf-8'))
        except Exception as e:
            print('card read error:', e); card = {}
        picks = [c for c in card.get('card', [])
                 if c.get('bet') and c.get('league') == 'nba'
                 and re.match(r'^\d{4}_\d{2}_\d{2}T', c.get('key') or '')]
        # collapse book mirrors: same match + same side + same game start = one bet
        seen_bets, uniq_picks = set(), []
        for c in picks:
            try:
                mm = re.match(r'^(\d{4})_(\d{2})_(\d{2})T(\d{2})_(\d{2})', c['key'])
                start = dt.datetime(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)),
                                    int(mm.group(4)), int(mm.group(5)), tzinfo=dt.timezone.utc)
            except Exception:
                continue
            sig = (parse_pair(c.get('match')), (c.get('side') or '').strip().upper(),
                   start.replace(second=0, microsecond=0))
            if sig in seen_bets:
                continue
            seen_bets.add(sig)
            c = dict(c); c['_start'] = start
            uniq_picks.append(c)
        # current bankroll = start + settled pnl
        settled = [o for o in ledger if o['status'] == 'settled']
        bankroll = BANKROLL_START + sum(o['pnl'] for o in settled)
        for c in uniq_picks:
            if c['key'] in known:
                continue
            try:
                start = c['_start']
                price = float(c['price'])
                if price >= 0:   # longshots: model edge unreliable, skip
                    continue
            except Exception:
                continue
            p_implied = implied_price(price)                 # what the contract costs
            p_model = (c.get('model_win') or 0) / 100.0      # what the model believes
            f = KELLY_FRACTION * kelly_fraction(p_model, p_implied)
            f = max(0.0, min(f, MAX_BET_FRAC))
            stake = max(MIN_STAKE, round(bankroll * f, 2)) if f > 0 else 0
            if stake < MIN_STAKE:
                continue
            contracts = int(stake / p_implied)
            if contracts < 1:
                continue
            new_orders.append({
                'key': c['key'], 'created': now.isoformat(timespec='seconds'),
                'match': c.get('match'), 'league': 'nba', 'market': c.get('market'),
                'pick': c.get('side'), 'book_price': price,
                'model_win': c.get('model_win'), 'ev': c.get('ev'),
                'kalshi_price': round(p_implied, 4), 'contracts': contracts,
                'stake': round(contracts * p_implied, 2),
                'fee_est': round(fee_per_contract(p_implied) * contracts, 2),
                'kelly_frac': round(f, 4), 'start': start.isoformat(),
                'status': 'open',
            })
        if new_orders:
            with open(LEDGER, 'a', encoding='utf-8') as fh:
                for o in new_orders:
                    fh.write(json.dumps(o) + '\n')
            ledger = load_ledger()
            known = {o['key'] for o in ledger}

        # ---- 2) settle matured picks ----
        index = build_feed_index()
        changed = False
        for o in ledger:
            if o['status'] != 'open':
                continue
            start = dt.datetime.fromisoformat(o['start'])
            if now < start + dt.timedelta(minutes=SETTLE_AFTER_MIN):
                continue
            g = find_game(index, parse_pair(o['match']), start)
            res = grade(o['pick'], g['pts']) if g else None
            p = o['kalshi_price']; c = o['contracts']
            if res in ('win', 'loss'):
                o['status'] = 'settled'; o['result'] = res
                fee = fee_per_contract(p) * c
                o['pnl'] = round(c * (1 - p) - fee if res == 'win' else -c * p - fee, 2)
                o['settled'] = now.isoformat(timespec='seconds')
                o['realScore'] = g['score']; o['realTeams'] = g['teams']
                changed = True; settled_now.append(o)
            elif res == 'push':
                o['status'] = 'settled'; o['result'] = 'push'
                o['pnl'] = round(-o['fee_est'], 2); changed = True
            elif g is None and now > start + dt.timedelta(hours=30):
                o['status'] = 'expired_unmatched'; changed = True
        if changed:
            with open(LEDGER, 'w', encoding='utf-8') as f:
                for o in ledger:
                    f.write(json.dumps(o) + '\n')

    # ---- 3) report ----
    ledger = load_ledger()
    open_o = [o for o in ledger if o['status'] == 'open']
    done = [o for o in ledger if o['status'] == 'settled']
    wins = sum(1 for o in done if o['result'] == 'win')
    losses = sum(1 for o in done if o['result'] == 'loss')
    cost = sum(o['stake'] for o in done)
    net = sum(o['pnl'] for o in done)
    bankroll = BANKROLL_START + net
    opencost = sum(o['stake'] for o in open_o)
    json.dump({'updated': now.isoformat(timespec='seconds'), 'bankroll_start': BANKROLL_START,
               'open': len(open_o), 'settled': len(done), 'wins': wins, 'losses': losses,
               'cost_settled': round(cost, 2), 'pnl': round(net, 2),
               'bankroll_now': round(bankroll, 2), 'open_cost': round(opencost, 2)},
              open(STATE, 'w'), indent=2)
    print('MODEL paper trader @', now.strftime('%Y-%m-%d %H:%M UTC'))
    print(f"  new model picks     : {len(new_orders)}")
    print(f"  settled this run    : {len(settled_now)}")
    print(f"  open model positions: {len(open_o)}  (cost ${opencost:,.2f})")
    print(f"  paper bankroll      : ${bankroll:,.2f} (start $1,000)")
    if done:
        wr = wins / max(1, wins + losses)
        print(f"  model record        : {wins}W-{losses}L = {wr*100:.1f}%   P&L ${net:+,.2f} on ${cost:,.2f}")
    for o in new_orders[:12]:
        print(f"    + {o['match']} | {o['pick']} @ {o['book_price']} | model {o['model_win']}% -> {o['contracts']} contracts @ {o['kalshi_price']*100:.0f}c (kelly {o['kelly_frac']*100:.1f}%)")
    for o in settled_now[:12]:
        print(f"    = {o['match']} | {o['pick']} -> {o['result'].upper()} ${o['pnl']:+.2f}")


if __name__ == '__main__':
    main(report_only='--report' in sys.argv)
