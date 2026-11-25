#!/usr/bin/env python
"""esb_picker.py — EsportsBattle EV paper picks (fixtures + BetsAPI odds -> card).

Isolated self-owned stream (skill rule): writes ONLY history/esb_* files.
  python esb_picker.py pick      # new paper picks on upcoming Battle games
  python esb_picker.py settle    # grade matured picks vs events/ended
  python esb_picker.py report    # ledger summary
"""
import json, os, re, sys, time, urllib.request, urllib.error
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esb_model

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR') or os.path.join(HERE, 'history')
LEDGER = os.path.join(HIST, 'esb_paper_ledger.jsonl')
TOK = (os.environ.get('BETSAPI_KEY') or '').strip() or open(os.path.join(HERE, 'betsapi_key.txt'), encoding='utf-8').read().strip()
UA = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://betsapi.com/'}
# 37298 = bet365 "Esoccer H2H GG League - 8 mins play" — the SIS H2H GG feed
# under its bet365 label. Added 2026-11-16 for the user's bet365 picks stream.
# NOT confirmed carried by BetMGM/Borgata under this label: picker rows for
# this league are tagged book='bet365' only (board shows a single bet365 row).
LEAGUES = {22614: 'battle8', 38439: 'volta6', 37298: 'h2hggl8'}
# Both leagues are carried by BetMGM/Borgata (volta6 = MGM's "Battle Volta
# Champions League A 2x3 Minutes" — verified Nov 13 from the user's own
# esoccer-108 page link; an earlier "not carried" conclusion was wrong: the
# games were simply outside the 60-min link window). Links attach as each
# game approaches start.
PICK_LEAGUES = dict(LEAGUES)
EV_BAR = 0.05
STATE = os.path.join(HIST, 'esb_paper_state.json')
KELLY_FRAC = 0.25     # quarter-Kelly
MIN_STAKE = 5.0
MAX_STAKE_PCT = 0.025  # cap at 2.5% of the paper bankroll per bet


def bankroll():
    if os.path.exists(STATE):
        try:
            return float(json.load(open(STATE)).get('bankroll', 1000.0))
        except Exception:
            pass
    return 1000.0


def save_bankroll(v):
    json.dump({'bankroll': round(v, 2)}, open(STATE, 'w'))


def kelly_stake(p, dec, br):
    """Quarter-Kelly stake: f = (p*dec - 1)/(dec - 1); clamped to [MIN, cap]."""
    if dec <= 1:
        return MIN_STAKE
    f = (p * dec - 1) / (dec - 1)
    stake = br * KELLY_FRAC * f
    return round(max(MIN_STAKE, min(stake, br * MAX_STAKE_PCT)), 2)


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
            time.sleep(4 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(4 * (i + 1))


def parse_team(name):
    m = re.match(r'^(.*?)\s*\((.+?)\)$', (name or '').strip())
    return (m.group(1), m.group(2).upper()) if m else (name or '', '')


def dec_to_am(d):
    d = float(d)
    return round(100 * (d - 1)) if d >= 2 else round(-100 / (d - 1))


def league_fit(league):
    games = [g for g in esb_model.load_games() if g['league'] == league]
    enc = esb_model.Enc(games)
    w = esb_model.fit_ridge(enc, games)
    pool = [(g['sa'] - esb_model.predict_side(w, enc, g['a'], g['b'], g['ta'], g['tb']),
             g['sb'] - esb_model.predict_side(w, enc, g['b'], g['a'], g['tb'], g['ta']))
            for g in games]
    return enc, w, pool


def pick():
    open_rows = {}
    if os.path.exists(LEDGER):
        for l in open(LEDGER, encoding='utf-8'):
            r = json.loads(l)
            if r.get('status') == 'open':
                open_rows[r['event_id']] = r
    fits = {}
    n_new = 0
    with open(LEDGER, 'a', encoding='utf-8') as f:
        for lid, lname in PICK_LEAGUES.items():
            d = get(f'https://api.betsapi.com/v1/events/upcoming?token={TOK}'
                    f'&sport_id=1&league_id={lid}')
            res = [e for e in (d.get('results') or []) if e.get('time_status') == '0']
            if not res:
                continue
            if lname not in fits:
                try:
                    fits[lname] = league_fit(lname)
                except Exception as e:
                    print(f'fit fail {lname}: {e}')
                    continue
            enc, w, pool = fits[lname]
            for e in res:
                eid = str(e.get('id'))
                if eid in open_rows:
                    continue
                h, a = e.get('home') or {}, e.get('away') or {}
                ta, pa = parse_team(h.get('name'))
                tb, pb = parse_team(a.get('name'))
                if not (pa and pb and ta and tb):
                    continue
                fc = esb_model.forecast(w, enc, {'a': pa, 'b': pb, 'ta': ta, 'tb': tb}, pool)
                if fc is None:
                    continue
                mu_a, mu_b, sa, sb = fc
                p = esb_model.probs_from_scores(sa, sb)
                od = get(f'https://api.betsapi.com/v1/event/odds?token={TOK}&event_id={eid}')
                odds = od.get('results') or {}
                # full odds snapshot for the card (American odds, bet365 feed line)
                odds_snap = {}
                for s in (odds.get('1_1') or [])[-1:]:
                    odds_snap['home'] = dec_to_am(s['home_od'])
                    odds_snap['draw'] = dec_to_am(s['draw_od'])
                    odds_snap['away'] = dec_to_am(s['away_od'])
                for s in (odds.get('1_3') or [])[-1:]:
                    try:
                        parts = [float(x) for x in (s.get('handicap') or '').split(',')]
                        odds_snap['line'] = sum(parts) / len(parts)
                        odds_snap['over'] = dec_to_am(s['over_od'])
                        odds_snap['under'] = dec_to_am(s['under_od'])
                    except ValueError:
                        pass
                # 1X2: latest snapshot of source 1_1
                cands = []
                for snap in (odds.get('1_1') or [])[-1:]:
                    cands.append(('home', '3way', float(snap['home_od']), p['home'], h.get('name')))
                    cands.append(('draw', '3way', float(snap['draw_od']), p['draw'], 'Draw'))
                    cands.append(('away', '3way', float(snap['away_od']), p['away'], a.get('name')))
                # totals: latest snapshot of 1_3 (handicap can be a split line "4.0,4.5")
                for snap in (odds.get('1_3') or [])[-1:]:
                    try:
                        parts = [float(x) for x in (snap.get('handicap') or '').split(',')]
                        line = sum(parts) / len(parts)
                    except ValueError:
                        continue
                    po, pp = p['total'](line)
                    cands.append(('over', f'Total Over {line}', float(snap['over_od']), po, f'O{line}'))
                    cands.append(('under', f'Total Under {line}', float(snap['under_od']),
                                  1 - po - pp, f'U{line}'))
                # --- selection gates (2026-11-14, from 107-pick calibration audit) ---
                # (1) SKIP TOTALS: the ratings->totals conversion is miscalibrated
                #     (model claimed .641 on picks realizing .250; -63% ROI on 20 bets).
                # (2) model_p must beat the NO-VIG market by >= 0.15, not just the raw
                #     vig: realized outcomes matched no-vig exactly (0.374 vs 0.375),
                #     so beating the vig alone is fantasy edge. Historical: +36.5% ROI
                #     (n=14, positive in both time halves) vs -13.6% unfiltered.
                # EV_BAR and Kelly sizing are unchanged.
                cands = [c for c in cands if c[0] not in ('over', 'under')]
                s11 = (odds.get('1_1') or [])[-1:]
                if s11:
                    try:
                        sn0 = s11[0]
                        iv = [1.0 / float(sn0['home_od']), 1.0 / float(sn0['draw_od']),
                              1.0 / float(sn0['away_od'])]
                        tot_iv = sum(iv)
                        novig = {'home': iv[0] / tot_iv, 'draw': iv[1] / tot_iv,
                                 'away': iv[2] / tot_iv}
                        cands = [c for c in cands if c[3] - novig[c[0]] >= 0.15]
                    except (KeyError, TypeError, ValueError, ZeroDivisionError):
                        pass  # odds feed malformed this tick: skip gating, EV_BAR still applies
                for kind, market, dec, prob, label in cands:
                    if not dec or dec <= 1 or prob <= 0:
                        continue
                    ev = prob * dec - 1
                    if ev >= EV_BAR:
                        stake = kelly_stake(prob, dec, bankroll())
                        row = {'event_id': eid, 'league': lname, 'time': e.get('time'),
                               'home': h.get('name'), 'away': a.get('name'),
                               'market': market, 'side': label, 'price_dec': dec,
                               'price_am': dec_to_am(dec), 'model_p': round(prob, 4),
                               'ev': round(ev, 4), 'stake': stake,
                               'odds_snapshot': odds_snap,
                               'status': 'open', 'placed': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                        f.write(json.dumps(row) + '\n')
                        n_new += 1
                        print(f"NEW {lname} | {h.get('name')} v {a.get('name')} | {market} {label} "
                              f"@{dec_to_am(dec)} | p={prob:.2f} EV={ev:+.1%} | stake ${stake}")
                time.sleep(1.0)
    print(f'new picks: {n_new}')


def settle():
    rows = [json.loads(l) for l in open(LEDGER, encoding='utf-8')] if os.path.exists(LEDGER) else []
    ended = {}
    for lid in LEAGUES:
        page = 1
        while page <= 10:
            d = get(f'https://api.betsapi.com/v1/events/ended?token={TOK}'
                    f'&sport_id=1&league_id={lid}&page={page}')
            res = d.get('results') or []
            if not res:
                break
            for e in res:
                ended[str(e.get('id'))] = e
            if len(res) < 50:
                break
            page += 1
    out = []
    n_settled = 0
    just = set()
    for r in rows:
        if r['status'] != 'open' or r['event_id'] not in ended:
            out.append(r)
            continue
        e = ended[r['event_id']]
        m = re.match(r'\s*(\d+)\s*-\s*(\d+)\s*$', e.get('ss') or '')
        if not m:
            out.append(r)
            continue
        just.add(r['event_id'])
        hs, as_ = int(m.group(1)), int(m.group(2))
        mk, side = r['market'], r['side']
        if mk == '3way':
            # side is the TEAM NAME (as picked), not a 'home'/'away' keyword —
            # the old literal-string comparison graded every 3way bet as a loss.
            if side == r.get('home'):
                win = hs > as_
            elif side == r.get('away'):
                win = as_ > hs
            elif side.strip().lower() == 'draw':
                win = hs == as_
            else:
                win = False   # unknown side label: leave as loss rather than guess
        else:
            line = float(re.findall(r'[\d.]+', mk)[-1])
            tot = hs + as_
            win = (side.startswith('O') and tot > line) or (side.startswith('U') and tot < line)
            if tot == line:
                r.update({'status': 'push', 'pnl': 0.0})
                n_settled += 1
                out.append(r)
                continue
        pnl = round(r['stake'] * (r['price_dec'] - 1), 2) if win else -r['stake']
        r.update({'status': 'win' if win else 'loss', 'pnl': pnl, 'final': f'{hs}-{as_}'})
        n_settled += 1
        out.append(r)
    # expire ancient open picks: games long past that BetsAPI's ended pages
    # no longer return can never settle — mark them so the board stops showing them
    now_ts = time.time()
    changed = False
    kept = []
    for r in out:
        if r['status'] == 'open' and r.get('time'):
            try:
                if int(r['time']) < now_ts - 36 * 3600:
                    r['status'] = 'expired_unmatched'
                    r['pnl'] = 0.0
                    changed = True
            except Exception:
                pass
        kept.append(r)
    out = kept
    if changed:
        n_settled += sum(1 for r in out if r['status'] == 'expired_unmatched')
    with open(LEDGER, 'w', encoding='utf-8') as f:
        for r in out:
            f.write(json.dumps(r) + '\n')
    # compound the paper bankroll by this batch's settled P&L
    pnl_batch = sum(r.get('pnl') or 0 for r in out if r['event_id'] in just)
    save_bankroll(bankroll() + pnl_batch)
    print(f'settled: {n_settled} | open: {sum(1 for r in out if r["status"] == "open")} '
          f'| bankroll ${bankroll():.2f}')


def report():
    if not os.path.exists(LEDGER):
        print('no ledger yet')
        return
    rows = [json.loads(l) for l in open(LEDGER, encoding='utf-8')]
    done = [r for r in rows if r['status'] in ('win', 'loss', 'push')]
    wins = sum(1 for r in done if r['status'] == 'win')
    pnl = sum(r.get('pnl', 0) for r in rows)
    staked = sum(r['stake'] for r in done)
    print(f'paper record: {wins}W-{sum(1 for r in done if r["status"]=="loss")}L-'
          f'{sum(1 for r in done if r["status"]=="push")}P | pnl ${pnl:+.2f} '
          f'on ${staked:.0f} staked (ROI {100*pnl/max(staked,0.01):+.1f}%) | '
          f'open: {sum(1 for r in rows if r["status"] == "open")}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'report'
    {'pick': pick, 'settle': settle, 'report': report}.get(cmd, report)()
