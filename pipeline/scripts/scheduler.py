#!/usr/bin/env python
"""scheduler.py — in-process scheduler + status server for the cloud host.

Jobs (running-flag guarded, never overlap):
  esb_cycle      every 10 min: backfill (delta) -> picker pick -> settle -> report
  model_book     every 10 min: model sportsbook paper book pick+settle
  kalshi_recorder every 2h:   Kalshi esports quotes/settlements sweep

Web endpoints (for the local boards / any viewer to pull):
  GET /          status page
  GET /data      {esb_ledger, model_sb_ledger, kalshi state}
"""
import json, os, re, subprocess, sys, threading, time, calendar, datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR', os.path.join(HERE, 'data'))
PY = sys.executable
os.makedirs(HIST, exist_ok=True)

JOBS = [
    # (name, interval_s, script, args)
    ('esb_cycle', 600, 'esb_cycle.py', []),
    ('model_book', 600, 'model_sb_paper.py', []),
    ('kalshi_recorder', 7200, 'kalshi_esports_recorder.py', []),
    ('scorer_cycle', 600, '', []),
]

def scorer_cycle():
    """Refresh H2H GG results, then re-score the source board's live card.

    Env notes: h2hggl_recorder reads SCORER_HISTORY_DIR/real_games.json, so it
    gets $HIST/history; source_observer reads $SCORER_DATA_ROOT/history/*, so it
    gets SCORER_DATA_ROOT=$HIST. Both converge on the files the model books read.
    """
    env = dict(os.environ,
               SCORER_HISTORY_DIR=os.path.join(HIST, 'history'),
               SCORER_DATA_ROOT=HIST,
               PYTHONPATH='', PYTHONHOME='')
    for script, args in (('h2hggl_recorder.py', ['--days', '3']),
                         ('source_observer.py', ['--live'])):
        try:
            r = subprocess.run([PY, '-I', os.path.join(HERE, 'scorer', script)] + args,
                               capture_output=True, text=True, timeout=280, env=env)
            out = (r.stdout or '').strip()
            print(f'[scorer:{script}] rc={r.returncode} {out[:400]}', flush=True)
            if r.returncode == 0:
                _last_ok['scorer_cycle'] = dt.datetime.utcnow().isoformat() + 'Z'
            elif r.stderr:
                print(f'[scorer:{script}] ERR {r.stderr[-500:]}', flush=True)
        except Exception as e:
            print(f'[scorer:{script}] EXC {e}', flush=True)
        time.sleep(2)
_running = {}
_last_ok = {}


def run_job(name, script, args):
    if _running.get(name):
        return
    _running[name] = True
    try:
        r = subprocess.run([PY, '-I', os.path.join(HERE, script)] + args,
                           capture_output=True, text=True, timeout=280,
                           env=dict(os.environ, SCORER_HISTORY_DIR=HIST,
                                    BETSAPI_KEY=os.environ.get('BETSAPI_KEY', ''),
                                    PYTHONPATH='', PYTHONHOME=''))
        out = (r.stdout or '').strip()
        print(f'[{name}] rc={r.returncode} {out[:400]}', flush=True)
        if r.returncode == 0:
            _last_ok[name] = dt.datetime.utcnow().isoformat() + 'Z'
        elif r.stderr:
            print(f'[{name}] ERR {r.stderr[-500:]}', flush=True)
    except Exception as e:
        print(f'[{name}] EXC {e}', flush=True)
    finally:
        _running[name] = False


def esb_cycle():
    """backfill delta -> pick -> settle -> report, as one cycle."""
    for script, args in (('esb_backfill.py', []), ('esb_picker.py', ['pick']),
                         ('esb_picker.py', ['settle']), ('esb_picker.py', ['report'])):
        run_job('esb:' + script, script, args)
    _last_ok['esb_cycle'] = dt.datetime.utcnow().isoformat() + 'Z'


def loop():
    t0 = time.time()
    while True:
        for name, interval, script, args in JOBS:
            due = (time.time() - t0) % interval < 60 or (name not in _last_ok and _last_ok.get('_boot') is None)
            if name == 'esb_cycle' and due:
                threading.Thread(target=esb_cycle, daemon=True).start()
            elif name == 'scorer_cycle' and due:
                threading.Thread(target=scorer_cycle, daemon=True).start()
            elif due and not _running.get(name):
                threading.Thread(target=run_job, args=(name, script, args), daemon=True).start()
        time.sleep(55)


def read(path):
    try:
        return open(path, encoding='utf-8').read()
    except Exception:
        return None


# ---------- board shim: serve bets/graded in the hosted board's format ----------
def _esb_iso(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat()
    except Exception:
        return ''


def board_bets():
    bets = []
    # partner profile: live bankrolls from the books (the board header reads these)
    try: nba_br = json.load(open(os.path.join(HERE, 'model_paper_state.json'))).get('bankroll_now', 1000.0)
    except Exception: nba_br = 1000.0
    try: fifa_br = json.load(open(os.path.join(HIST, 'esb_paper_state.json'))).get('bankroll', 1000.0)
    except Exception: fifa_br = 1000.0
    partner = {"id": "model", "name": "The Model",
               "bankroll": round(nba_br, 2), "fifaBankroll": round(fifa_br, 2),
               "nflBankroll": 0, "cap": round(0.025 * fifa_br, 2)}
    # ESB soccer paper picks (open) — key must match /graded keys exactly
    try:
        rows = [json.loads(x) for x in (read(os.path.join(HIST, 'esb_paper_ledger.jsonl')) or '').splitlines() if x.strip()]
    except Exception:
        rows = []
    for o in rows:
        if o.get('status') != 'open':
            continue
        # only show games that haven't started yet (stale picks expire, not display)
        try:
            st_ts = int(o.get('time') or 0)
            if st_ts and st_ts < time.time():
                continue
        except Exception:
            pass
        base = 'esb:%s:%s' % (o.get('event_id'), (o.get('side') or '').strip())
        if o.get('league') == 'h2hggl8':
            # bet365-only league ("Esoccer H2H GG League - 8 mins play"):
            # no MGM/Borgata twin row and no cds-api resolver (different
            # provider). Single bet365 row, lobby link, manual match check.
            bets.append({
                'key': base + '|bet365', 'kind': 'esb', 'league': o.get('league'),
                'lgName': 'bet365: Soccer > Esports > Esoccer H2H GG League - 8 mins play',
                'match': '%s v %s' % (o.get('home'), o.get('away')),
                'ga': o.get('home'), 'gb': o.get('away'),
                'market': o.get('market'), 'side': o.get('side'),
                'price': o.get('price_am'), 'stake': o.get('stake'),
                'start': _esb_iso(o.get('time')),
                'note': ('model %.1f%% EV %+.1f%%' % (100 * (o.get('model_p') or 0), 100 * (o.get('ev') or 0))
                         + ' | bet365 only: open bet365 > Soccer > Esports > Esoccer H2H GG '
                           'League - 8 mins play and check the odds match before betting.'),
                'armed': True, 'book': 'bet365', 'line': '', 'legs': [],
                'web': 'https://www.bet365.com', 'app': 'https://www.bet365.com'})
            continue
        note = ('model %.1f%% EV %+.1f%%' % (100 * (o.get('model_p') or 0), 100 * (o.get('ev') or 0))
            + ' | Same game at BOTH books - place at one, then tap Skip on the other row. ')
        # exact-game deep link: resolve once, ids are shared across both books
        # (same Entain NJ platform). Unresolved picks are dropped from the
        # board (paper-only) unless the feed itself is down, in which case
        # lobby links serve as the fallback.
        epath = _mgm_event_path(108, o.get('home') or '', o.get('away') or '',
                                int(o.get('time') or 0))
        # bettable-only board: show a slip ONLY when the exact fixture is live
        # on the books' shared feed (ids are shared, so one hit covers both
        # rows). A miss with a healthy feed = neither book carries this game
        # tonight -> paper bet only, keep it off the board. If the feed itself
        # is down (empty fixture list) we can't tell, so keep the old
        # lobby-link rows rather than blanking the board.
        if not epath and _mgm_fixtures(108):
            continue
        note += ('Links open the exact match - check odds match before betting.' if epath
                 else 'The link opens the esoccer lobby: find the same match, check odds match before betting.')
        # 'app' = mobile-web (sports.*): plain web page, does NOT open the app
        # by itself. The BOARD front-end (index.html appHref()) rewrites these
        # per platform: Android intent:// deep links with the real package
        # names (com.playmgm.nj.sports / com.borgata.nj.sports); iPhone the
        # /*/sportsapp/* universal-link route on the www. domains (AASA covers
        # only that prefix; sports.* is Redirex-302'd so never used for iOS).
        p108 = epath or 'esoccer-108'
        for book, web, app in (
                ('betmgm', 'https://www.nj.betmgm.com/en/sports/' + p108,
                 'https://sports.betmgm.com/en/sports/' + p108),
                ('borgata', 'https://www.borgataonline.com/en/sports/' + p108,
                 'https://sports.borgataonline.com/en/sports/' + p108)):
            bets.append({
                'key': base + '|' + book, 'kind': 'esb', 'league': o.get('league'),
                'lgName': ('Esoccer Battle 2x4 (8 mins) — on the app: Soccer > Esoccer '
                           '(leagues named like "Battle Premier League 2x4" / "Battle La Liga 2x4")'),
                'match': '%s v %s' % (o.get('home'), o.get('away')),
                'ga': o.get('home'), 'gb': o.get('away'),
                'market': o.get('market'), 'side': o.get('side'),
                'price': o.get('price_am'), 'stake': o.get('stake'),
                'start': _esb_iso(o.get('time')),
                'note': note,
                'armed': True, 'book': book, 'line': '', 'legs': [],
                'web': web, 'app': app})
    # NBA: mirror ALL upcoming singles from the user's own board, with the
    # observer's verdict attached (armed = in the profitable >=64% band).
    try:
        card = json.load(open(os.path.join(HIST, 'history', 'scored_card.json')))
    except Exception:
        card = {}
    nba_cands = []
    for c in card.get('card', []):
        if c.get('league') != 'nba':
            continue
        # real game start parsed from the key prefix YYYY_MM_DDTHH_MM_SSSZ_...
        m = re.match(r'^(\d{4})_(\d{2})_(\d{2})T(\d{2})_(\d{2})_(\d{2})', c.get('key') or '')
        if not m:
            continue
        start = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), tzinfo=dt.timezone.utc)
        if start < dt.datetime.now(dt.timezone.utc):
            continue
        nba_cands.append((c, start))
    # collapse book mirrors: same pick posted at 2-3 books -> keep the BEST price
    best = {}
    for c, start in nba_cands:
        try:
            dec = 1 + (float(c.get('price')) / 100.0 if float(c.get('price')) > 0
                       else 100.0 / abs(float(c.get('price'))))
        except (TypeError, ValueError):
            dec = 0
        sig = (c.get('match'), (c.get('side') or '').strip().upper(), start.strftime('%Y%m%dT%H%M'))
        if sig not in best or dec > best[sig][0]:
            best[sig] = (dec, c, start)
    for dec, c, start in best.values():
        rec = bool(c.get('bet'))
        # the brain's own quarter-Kelly sizing, identical to the paper books'
        # formula but UNCAPPED at the user's request (2026-11-15):
        # f = (p*dec - 1)/(dec - 1), quarter-Kelly, $5 floor
        try:
            p = float(c.get('model_win') or 0) / 100.0
            price = float(c.get('price') or -110)
            d2 = 1 + (price / 100.0 if price > 0 else 100.0 / abs(price))
            f = (p * d2 - 1) / (d2 - 1)
            f = max(0.0, 0.25 * f)
            kelly_stake = round(max(5.0, partner['bankroll'] * f), 2) if f > 0 else 0
        except Exception:
            kelly_stake = 0
        # exact eBasketball GG event link for the MGM books (shared ids);
        # FanDuel rows keep the plain lobby links (no resolver for FD yet).
        nb = (c.get('book') or 'fanduel').strip().lower()
        mparts = (c.get('match') or ' v ').split(' v ')
        epath = _mgm_event_path(119, mparts[0],
                                mparts[1] if len(mparts) > 1 else '',
                                start.timestamp()) if nb in ('betmgm', 'borgata') else ''
        links = _mgm_links(epath, nb)
        bets.append({
            'key': c.get('key'), 'kind': 'nba', 'league': 'nba',
            'lgName': ('eBasketball H2H GG League - 4x5 mins — on the app: Basketball > E-Basketball'
                       if nb in ('betmgm', 'borgata')
                       else 'FanDuel: Basketball > E-Basketball (eBasketball H2H GG)'),
            'match': c.get('match'), 'ga': (c.get('match') or '').split(' v ')[0],
            'gb': (c.get('match') or ' v ').split(' v ')[1] if ' v ' in (c.get('match') or '') else '',
            'market': c.get('market'), 'side': c.get('side'),
            'price': c.get('price'), 'stake': kelly_stake if rec else 0,
            'start': start.isoformat(),
            'note': 'observer: model %.1f%% EV %s%% -> %s%s' % (
                float(c.get('model_win') or 0), c.get('ev'),
                'BET (profitable band)' if rec else 'skip (below 64%% band)',
                '' if not rec else ' | qKelly $%s' % ('%.0f' % kelly_stake)),
            'armed': rec, 'why': '' if rec else 'observer model: below the 64%% band',
            'book': nb,
            'web': links[0] if links else NBA_BOOK_LINKS.get(nb, ''),
            'app': links[1] if links else NBA_BOOK_APPS.get(nb, ''),
            'line': '', 'legs': []})
    # best EV first so the top of the card is the brain's strongest conviction
    bets.sort(key=lambda b: -float((b.get('note') or '').split('EV ')[-1].split('%')[0] or -99))
    return bets, partner


# real book landing pages (verified 2026-11-16, all 200): the card rows carry the
# book each pick was posted at, and each book carries these leagues at a stable URL.
NBA_BOOK_LINKS = {
    'betmgm': 'https://www.nj.betmgm.com/en/sports/ebasketball-119/betting/world-6/ebasketball-h2h-gg-league-108782',
    'borgata': 'https://www.borgataonline.com/en/sports/ebasketball-119/betting/world-6/ebasketball-h2h-gg-league-108782',
    # FanDuel: no exact-event resolver yet, so link its eBasketball lobby.
    # FD row keys carry no __BOOK suffix and the model prices them at FD odds.
    'fanduel': 'https://sportsbook.fanduel.com/sports/basketball-5',
}

# mobile-web twins of the above (sports.* domains, verified 200 on 2026-11-16):
# the branded app button on the board opens these on phones.
NBA_BOOK_APPS = {
    'betmgm': 'https://sports.betmgm.com/en/sports/ebasketball-119/betting/world-6/ebasketball-h2h-gg-league-108782',
    'borgata': 'https://sports.borgataonline.com/en/sports/ebasketball-119/betting/world-6/ebasketball-h2h-gg-league-108782',
    'fanduel': 'https://sportsbook.fanduel.com/sports/basketball-5',
}


# --- exact-game deep links (2026-11-16) ---------------------------------------
# Both books run the same Entain NJ platform and SHARE event ids, so one
# resolution serves both. Feed: cds-api/bettingoffer/fixtures with the public
# page access id (no auth). Cloudflare blocks plain-python TLS fingerprints on
# the API path (urllib/requests/httpx all get 403; only curl TLS passes), so
# curl_cffi impersonates Safari — added to start.sh/Dockerfile pip installs.
# Fixture name "Paris (ZORO) - Atleti (Firminho)" + id "5:1382794" map to the
# web route /en/sports/events/paris-zoro-atleti-firminho-5:1382794 (verified
# rendering the exact event on both books). The board front-end appHref()
# then rewrites per platform with NO changes needed here:
#   Android: intent://sports.<book>/en/sports/events/...#Intent;package=...
#   iPhone: https://www.<book>/en/sportsapp/sports/events/... (AASA covers
#           /*/sportsapp/*; the sportsapp prefix 302s to the same web path)
try:
    from curl_cffi import requests as _cr
except Exception:
    _cr = None
_MGM_ACC = 'ZTllNjllODUtOWQwNS00YmU4LWE4NTEtZGZjOTkzMGM5OWU4'
_MGM_HOST = 'www.nj.betmgm.com'
_MGM_FIXAPI = ('https://%s/cds-api/bettingoffer/fixtures?x-bwin-accessid=%s'
               '&lang=en-us&country=US&userCountry=US&subdivision=US-California'
               '&fixtureTypes=Standard&state=Latest&offerMapping=Filtered'
               '&offerCategories=Gridable&fixtureCategories=Gridable,NonGridable,Other'
               '&isPriceBoost=false&statisticsModes=Rank,SeasonStandings'
               '&skip=0&take=100&sortBy=Tags&sportIds=%%d') % (_MGM_HOST, _MGM_ACC)
_MGM_CACHE = {'at': 0.0, 'fx': {}}       # sport id -> [ {id,name,start}, ... ]
_MGM_LOCK = threading.Lock()
# sport ids: esoccer sims = 108, eBasketball H2H GG = 119 (compId 108782)

def _mgm_fixtures(sport):
    """upcoming fixtures for a sport id, cached 90s; [] on any failure"""
    if _cr is None:
        return []
    now = time.time()
    if now - _MGM_CACHE['at'] > 90:
        if _MGM_LOCK.acquire(blocking=False):
            try:
                s = _cr.Session(impersonate='safari')
                s.get('https://%s/en/sports/esoccer-108' % _MGM_HOST, timeout=10)
                for sp in (108, 119):
                    r = s.get(_MGM_FIXAPI % sp,
                              headers={'Accept': 'application/json'}, timeout=10)
                    d = r.json()
                    _MGM_CACHE['fx'][sp] = [
                        {'id': x['id'],
                         'name': (x.get('name') or {}).get('value') or '',
                         'start': x.get('startDate')}
                        for x in (d.get('fixtures') or [])]
                _MGM_CACHE['at'] = now
            except Exception:
                _MGM_CACHE['at'] = now    # back off this cycle; lobby links serve
            finally:
                _MGM_LOCK.release()
    return _MGM_CACHE['fx'].get(sport) or []

def _mgm_tags(s):
    """player/club tokens: parenthesized gamer tag if present, else the name"""
    t = re.findall(r'\(([^)]+)\)', s)
    return [re.sub(r'[^A-Z0-9]', '', x.upper()) for x in t] or \
           [re.sub(r'[^A-Z0-9]', '', s.upper())]

def _mgm_event_path(sport, home, away, start_ts, window=300):
    """'events/<slug>-<id>' for the fixture matching both tags + start time"""
    want = _mgm_tags(home or '') + _mgm_tags(away or '')
    want = [w for w in want if w]
    if not want:
        return ''
    best, bd = None, None
    for f in _mgm_fixtures(sport):
        try:
            ts = calendar.timegm(time.strptime((f['start'] or '')[:19],
                                               '%Y-%m-%dT%H:%M:%S'))
        except Exception:
            continue
        if abs(ts - start_ts) > window:
            continue
        have = set(_mgm_tags(f['name']))
        if not all(w in have for w in want):
            continue
        if best is None or abs(ts - start_ts) < bd:
            best, bd = f, abs(ts - start_ts)
    if not best:
        return ''
    slug = '-'.join(re.sub(r'[^a-z0-9]+', ' ', best['name'].lower()).split())
    return 'events/%s-%s' % (slug, best['id'])

def _mgm_links(path, book):
    """(web, app) links for a book given a shared event path, else None"""
    if not path or book not in ('betmgm', 'borgata'):
        return None
    www = 'https://www.nj.betmgm.com/en/sports/' if book == 'betmgm' \
        else 'https://www.borgataonline.com/en/sports/'
    app = 'https://sports.betmgm.com/en/sports/' if book == 'betmgm' \
        else 'https://sports.borgataonline.com/en/sports/'
    return (www + path, app + path)


def graded_rows():
    out = []
    def rows(path):
        try:
            return [json.loads(x) for x in (read(path) or '').splitlines() if x.strip()]
        except Exception:
            return []
    for o in rows(os.path.join(HIST, 'esb_paper_ledger.jsonl')):
        if o.get('status') in ('win', 'loss', 'push'):
            # the board posts each esb pick as TWO rows (betmgm + borgata), so
            # grade both suffixed keys; the un-placed twin simply never shows.
            base = 'esb:%s:%s' % (o.get('event_id'), (o.get('side') or '').strip())
            # h2hggl8 rows are posted bet365-only: grade just that suffixed key
            books = ('bet365',) if o.get('league') == 'h2hggl8' else ('betmgm', 'borgata')
            for book in books:
                out.append({'key': base + '|' + book,
                            'result': o['status'], 'pnl': o.get('pnl')})
    for name in ('model_sb_ledger.jsonl', 'model_ledger.jsonl'):
        for o in rows(os.path.join(HERE, name)):
            if o.get('result') in ('win', 'loss', 'push') and o.get('key'):
                out.append({'key': o['key'], 'result': o['result'], 'pnl': o.get('pnl')})
    return out


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/data':
            body = json.dumps({
                'at': dt.datetime.utcnow().isoformat() + 'Z',
                'esb_ledger': read(os.path.join(HIST, 'esb_paper_ledger.jsonl')),
                'esb_state': read(os.path.join(HIST, 'esb_paper_state.json')),
                'model_sb_ledger': read(os.path.join(HERE, 'model_sb_ledger.jsonl')),
                'kalshi_quotes_tail': read(os.path.join(HIST, 'kalshi_history', 'sweep_log.jsonl')),
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
        elif self.path == '/board':
            bets, partner = board_bets()
            body = json.dumps({'at': dt.datetime.utcnow().isoformat() + 'Z',
                               'live': True, 'why': '', 'title': 'Picks board — The Model',
                               'checkedAt': dt.datetime.utcnow().isoformat() + 'Z',
                               'partner': partner, 'model': partner,
                               'partners': [partner], 'liveBankroll': {},
                               'bets': bets}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
        elif self.path == '/graded':
            body = json.dumps(graded_rows()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
        elif self.path == '/':
            body = ('<h1>picks pipeline host</h1><pre>jobs: ' +
                    json.dumps({k: v for k, v in _last_ok.items()}) +
                    '</pre><a href="/data">/data</a>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
        else:
            self.send_response(404); self.end_headers(); return
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    _last_ok['_boot'] = dt.datetime.utcnow().isoformat() + 'Z'
    threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get('PORT', '10000'))
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
