#!/usr/bin/env python
"""model_sb_paper.py — the MODEL's own picks as SPORTSBOOK paper bets.

Same intake as model_paper_trader (history/scored_card.json: the trained
scorer's flagged NBA singles), but priced with real book economics instead of
Kalshi contracts: stake at American odds, win pays stake*(100/|price|),
loss costs the stake, push refunds. Quarter-Kelly sized on (model_win, price),
$5 floor, 2% cap, bankroll compounds.

Answers: "if I bet the model's card at my books (FD/MGM/Borgata prices), what
does the record look like?" — the sportsbook twin of the Kalshi-style
model_ledger.

Ledger: kalshi-bot/model_sb_ledger.jsonl
Run:   python model_sb_paper.py            # add new picks + settle matured
       python model_sb_paper.py --report
"""
import json, os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model_paper_trader as mpt   # reuse: parse_pair, grade, feed join, card rules

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, 'model_sb_ledger.jsonl')
BANKROLL_START = 1000.0
KELLY_FRACTION = 0.25
MAX_BET_FRAC = 0.02
MIN_STAKE = 5.0


def am_to_dec(p):
    return 1 + (p / 100.0 if p > 0 else 100.0 / abs(p))


def sb_pnl(price, stake, result):
    if result == 'win':
        return round(stake * (price / 100.0 if price > 0 else 100.0 / abs(price)), 2)
    if result == 'loss':
        return -round(stake, 2)
    return 0.0


def main(report_only=False):
    now = dt.datetime.now(dt.timezone.utc)
    ledger = mpt.load_ledger() if LEDGER == mpt.LEDGER else (
        [json.loads(l) for l in open(LEDGER, encoding='utf-8') if l.strip()]
        if os.path.exists(LEDGER) else [])
    known = {o['key'] for o in ledger}

    if not report_only:
        # ---- 1) take the model's new picks off the live card ----
        try:
            card = json.load(open(mpt.CARD, encoding='utf-8'))
        except Exception as e:
            print('card read error:', e); card = {}
        import re
        picks = [c for c in card.get('card', [])
                 if c.get('bet') and c.get('league') == 'nba'
                 and re.match(r'^\d{4}_\d{2}_\d{2}T', c.get('key') or '')]
        seen, uniq = set(), []
        for c in picks:
            try:
                mm = re.match(r'^(\d{4})_(\d{2})_(\d{2})T(\d{2})_(\d{2})', c['key'])
                start = dt.datetime(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)),
                                    int(mm.group(4)), int(mm.group(5)), tzinfo=dt.timezone.utc)
            except Exception:
                continue
            sig = (mpt.parse_pair(c.get('match')), (c.get('side') or '').strip().upper(),
                   start.replace(second=0, microsecond=0))
            if sig in seen:
                continue
            seen.add(sig)
            c = dict(c); c['_start'] = start
            uniq.append(c)
        settled = [o for o in ledger if o.get('status') == 'settled']
        bankroll = BANKROLL_START + sum(o.get('pnl', 0) for o in settled)
        new_orders = []
        for c in uniq:
            if c['key'] in known:
                continue
            try:
                price = float(c['price'])
                if price >= 0:      # longshots: model edge unreliable — same rule as the kalshi book
                    continue
            except Exception:
                continue
            p_model = (c.get('model_win') or 0) / 100.0
            dec = am_to_dec(price)
            f = KELLY_FRACTION * ((p_model * dec - 1) / (dec - 1))
            f = max(0.0, min(f, MAX_BET_FRAC))
            if f <= 0:
                continue
            stake = max(MIN_STAKE, round(bankroll * f, 2))
            new_orders.append({
                'key': c['key'], 'created': now.isoformat(timespec='seconds'),
                'match': c.get('match'), 'league': 'nba', 'market': c.get('market'),
                'pick': c.get('side'), 'book_price': price,
                'model_win': c.get('model_win'), 'ev': c.get('ev'),
                'kelly_frac': round(f, 4), 'stake': stake,
                'start': c['_start'].isoformat(), 'status': 'open'})
        if new_orders:
            with open(LEDGER, 'a', encoding='utf-8') as fh:
                for o in new_orders:
                    fh.write(json.dumps(o) + '\n')
            ledger = [json.loads(l) for l in open(LEDGER, encoding='utf-8') if l.strip()]
            known = {o['key'] for o in ledger}
            for o in new_orders:
                print(f"NEW {o['match']} | {o['pick']} @{o['book_price']} "
                      f"| model {o['model_win']}% | stake ${o['stake']}")

        # ---- 2) settle matured picks vs the real feed ----
        index = mpt.build_feed_index()
        changed = False
        for o in ledger:
            if o['status'] != 'open':
                continue
            start = dt.datetime.fromisoformat(o['start'])
            if now < start + dt.timedelta(minutes=mpt.SETTLE_AFTER_MIN):
                continue
            g = mpt.find_game(index, mpt.parse_pair(o['match']), start)
            res = mpt.grade(o['pick'], g['pts']) if g else None
            if res in ('win', 'loss'):
                o['status'] = 'settled'; o['result'] = res
                o['pnl'] = sb_pnl(o['book_price'], o['stake'], res)
                o['settled'] = now.isoformat(timespec='seconds')
                o['realScore'] = g['score']; o['realTeams'] = g['teams']
                changed = True
            elif res == 'push':
                o['status'] = 'settled'; o['result'] = 'push'; o['pnl'] = 0.0
                changed = True
            elif g is None and now > start + dt.timedelta(hours=30):
                o['status'] = 'expired_unmatched'; changed = True
        if changed:
            with open(LEDGER, 'w', encoding='utf-8') as f:
                for o in ledger:
                    f.write(json.dumps(o) + '\n')

    # ---- 3) report ----
    done = [o for o in ledger if o.get('status') == 'settled']
    wins = [o for o in done if o.get('result') == 'win']
    losses = [o for o in done if o.get('result') == 'loss']
    pushes = [o for o in done if o.get('result') == 'push']
    pnl = sum(o.get('pnl', 0) for o in done)
    staked = sum(o['stake'] for o in done)
    bankroll = BANKROLL_START + pnl
    print(f"MODEL @ BOOKS: {len(wins)}W-{len(losses)}L-{len(pushes)}P "
          f"({100*len(wins)/max(len(done),1):.1f}%) | pnl ${pnl:+.2f} on ${staked:.0f} staked "
          f"-> {100*pnl/max(staked,.01):+.1f}% ROI | bankroll ${bankroll:.2f} "
          f"| open {sum(1 for o in ledger if o.get('status') == 'open')}")


if __name__ == '__main__':
    main(report_only='--report' in sys.argv)
