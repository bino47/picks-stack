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
import json, os, re, subprocess, sys, threading, time, calendar, base64
import datetime as dt
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get('SCORER_HISTORY_DIR', os.path.join(HERE, 'data'))
PY = sys.executable
os.makedirs(HIST, exist_ok=True)

# ---- pipeline ledger backup (GitHub branch, mirrors the board shim) --------
# /var/data and the service fs are BOTH ephemeral on the Render free tier:
# every deploy/spin-down wipes the paper ledgers, permanently orphaning any
# board placement whose pick has not settled yet. Snapshot them to the
# board-db branch (distinct filenames; the board only restores board.db.bak)
# and restore on boot when a local ledger is fresh. No-op without the token.
GH_TOKEN  = (os.environ.get('GH_BACKUP_TOKEN') or '').strip()
GH_REPO   = (os.environ.get('GH_BACKUP_REPO') or 'bino47/picks-stack').strip()
GH_BRANCH = (os.environ.get('GH_BACKUP_BRANCH') or 'board-db').strip()
LEDGER_FILES = {                      # remote path -> local path
    'pipeline/ledgers/esb_paper_ledger.jsonl':    os.path.join(HIST, 'esb_paper_ledger.jsonl'),
    'pipeline/ledgers/esb_bb_paper_ledger.jsonl': os.path.join(HIST, 'esb_bb_paper_ledger.jsonl'),
    'pipeline/ledgers/fifa_board_ledger.jsonl':   os.path.join(HIST, 'fifa_board_ledger.jsonl'),
    'pipeline/ledgers/model_sb_ledger.jsonl':     os.path.join(HERE, 'model_sb_ledger.jsonl'),
}
_last_pushed = {}

def _gh(path, method='GET', body=None, timeout=30):
    req = urllib.request.Request('https://api.github.com' + path,
        headers={'Authorization': 'token ' + GH_TOKEN,
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'},
        method=method, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or '{}')

def ledgers_restore():
    """Boot: restore any ledger that is missing/empty locally."""
    if not GH_TOKEN:
        return
    for rpath, lpath in LEDGER_FILES.items():
        try:
            local = os.path.exists(lpath) and os.path.getsize(lpath) > 0
            if local:
                continue
            st, d = _gh(f'/repos/{GH_REPO}/contents/{rpath}?ref={GH_BRANCH}')
            raw = base64.b64decode(d.get('content') or b'')
            if not raw:
                continue
            os.makedirs(os.path.dirname(lpath), exist_ok=True)
            with open(lpath, 'wb') as f:
                f.write(raw)
            _last_pushed[rpath] = raw
            print('ledger restore:', rpath, '->', lpath)
        except Exception as e:
            print('ledger restore skipped:', rpath, e)

def _ledger_backup_loop():
    """Every 10 min: push ledgers whose bytes changed since the last push."""
    if not GH_TOKEN:
        return
    while True:
        time.sleep(600)
        for rpath, lpath in LEDGER_FILES.items():
            try:
                if not (os.path.exists(lpath) and os.path.getsize(lpath)):
                    continue
                with open(lpath, 'rb') as f:
                    raw = f.read()
                if _last_pushed.get(rpath) == raw:
                    continue
                sha = None
                try:
                    st, d = _gh(f'/repos/{GH_REPO}/contents/{rpath}?ref={GH_BRANCH}')
                    sha = d.get('sha')
                    if base64.b64decode(d.get('content') or b'') == raw:
                        _last_pushed[rpath] = raw
                        continue        # remote already matches local
                except Exception:
                    sha = None
                body = {'message': 'pipeline ledger backup ' + rpath,
                        'content': base64.b64encode(raw).decode(),
                        'branch': GH_BRANCH}
                if sha:
                    body['sha'] = sha
                _gh(f'/repos/{GH_REPO}/contents/{rpath}', method='PUT', body=body)
                _last_pushed[rpath] = raw
            except Exception as e:
                print('ledger backup failed:', rpath, e)


JOBS = [
    # (name, interval_s, script, args)
    ('esb_cycle', 600, 'esb_cycle.py', []),
    ('model_book', 600, 'model_sb_paper.py', []),
    ('bb_cycle', 600, 'esb_bb_cycle.py', []),
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
                         ('betsapi_feed.py', ['--days', '3', '--pages', '3']),
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


def read_tail(path, n=400):
    """Last n lines of a jsonl ledger — ledgers grow unbounded and would
    bloat the /board payload; the Sims tab only needs recent + graded rows."""
    try:
        lines = open(path, encoding='utf-8').read().splitlines()
        return '\n'.join(lines[-n:])
    except Exception:
        return None


# ---------- board shim: serve bets/graded in the hosted board's format ----------
def _am2dec(v):
    """American odds -> decimal, or None."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 0:
        return 1 + v / 100.0
    if v < 0:
        return 1 + 100.0 / abs(v)
    return None


def _ev_at(p, am):
    """EV FRACTION of betting price `am` at model prob p (0-1); None if unknown."""
    d = _am2dec(am)
    if d is None or p is None:
        return None
    return float(p) * d - 1.0


def _q_am(q):
    """bet365 quote -> American price, for either quote shape:
    esb quotes are (am, quote_time) tuples, bb quotes are {'am': ...} dicts."""
    if isinstance(q, dict):
        return q.get('am')
    if isinstance(q, (tuple, list)) and q:
        return q[0]
    return None


def _esb_note_ev(o, q):
    """EV fraction for a bet365 twin note: recompute at the REAL quoted price
    when bet365's odds differ from the card (the card's own ev is priced at the
    source book) — the displayed price and the displayed EV must agree.
    Works across ledger schemas: esb rows carry model_p, bb rows p_model."""
    am = _q_am(q)
    if am is not None:
        p = o.get('model_p')
        if p is None:
            p = o.get('p_model')
        ev = _ev_at(p, am)
        if ev is not None:
            return ev
    return o.get('ev') or 0


def _esb_iso(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat()
    except Exception:
        return ''


# --- FanDuel FIFA (esoccer) picks ---------------------------------------------
# The observer scores the friend board's FIFA rows (FIFA_<hash>_* keys). To put
# them on OUR board with one-click FanDuel slips, join those keys back to the
# friend's live /data, which carries the addToBetslip URLs, real club names and
# tip time. Bettable-only: no friend row = nothing to point at = no row.
FRIEND_BASE = "https://picks.001msk001.cc/p/ZKHxK1Wrkl88RjbayhBkSaUspEjM_fqw"
_FRIEND = {'at': 0.0, 'data': None}


def _friend_card(max_age=120):
    now = time.time()
    if _FRIEND['data'] is not None and now - _FRIEND['at'] < max_age:
        return _FRIEND['data']
    try:
        req = urllib.request.Request(FRIEND_BASE + '/data',
                                     headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        _FRIEND.update(at=now, data=data)
        return data
    except Exception:
        return None


def _grade_fifa(o, games_by_date):
    """win/loss/push for one posted FIFA pick, or None while ungraded.

    Matches the finished H2H-feed game by unordered alias pair + nearest start
    within 10 min (esoccer replays the same pairing several times a day).
    Finished-game entries store aliases SORTED, so they do NOT align with the
    score array — map each alias to its score index via the club brand in
    fdName ("Napoli (EXECUTIONER) ...") against the entry's teams.
    """
    try:
        st = dt.datetime.fromisoformat((o.get('start') or '').replace('Z', '+00:00'))
    except Exception:
        return None
    if st > dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20):
        return None  # tip + ~15 min match: not finished yet
    ga_u, gb_u = str(o.get('ga') or '').upper(), str(o.get('gb') or '').upper()
    if not ga_u or not gb_u or ga_u == gb_u:
        return None
    best = None
    # The feed buckets games under the SCHEDULE date (US Eastern), so a
    # late-night UTC start can sit in the previous day's bucket — check both.
    day = dt.timedelta(days=1)
    pool = (games_by_date.get(st.strftime('%Y-%m-%d')) or []) \
         + (games_by_date.get((st - day).strftime('%Y-%m-%d')) or []) \
         + (games_by_date.get((st + day).strftime('%Y-%m-%d')) or [])
    for g in pool:
        if set(g.get('aliases') or []) != {ga_u, gb_u}:
            continue
        if (g.get('status') or '') != 'MATCH_ENDED':
            continue
        try:
            gst = dt.datetime.fromisoformat(str(g.get('start') or '').replace('Z', '+00:00'))
        except Exception:
            continue
        dmin = abs((gst - st).total_seconds()) / 60.0
        if dmin <= 10 and (best is None or dmin < best[0]):
            best = (dmin, g)
    if not best:
        return None
    g = best[1]
    score = g.get('score') or []
    if len(score) != 2:
        return None
    fdn = str(o.get('fdName') or '')
    idx = {}
    # split on the versus separator so the second alias's brand text can't
    # swallow " v " — each half is "<Brand> (<Alias>)".
    for half in re.split(r'\s+v\s+', fdn):
        m = re.search(r'\(\s*([A-Za-z0-9 ]+)\s*\)', half)
        if not m:
            continue
        alias = m.group(1).strip().upper()
        brand = half[:m.start()].strip().upper()
        if alias in (ga_u, gb_u) and brand:
            for i, t in enumerate(g.get('teams') or []):
                if str(t).strip().upper() == brand:
                    idx[alias] = i
                    break
    if ga_u not in idx or gb_u not in idx or idx[ga_u] == idx[gb_u]:
        return None
    marg = score[idx[ga_u]] - score[idx[gb_u]]  # ga goals minus gb goals
    side = (o.get('side') or '').strip()
    m = re.match(r'(?i)^(Over|Under)\s+([\d.]+)$', side)
    if m:
        tot = score[idx[ga_u]] + score[idx[gb_u]]
        line = float(m.group(2))
        if tot > line:
            return 'win' if m.group(1).lower() == 'over' else 'loss'
        if tot < line:
            return 'loss' if m.group(1).lower() == 'over' else 'win'
        return 'push'
    m = re.match(r'(?i)^(%s|%s)\s+([+-]?[\d.]+)$' % (re.escape(ga_u), re.escape(gb_u)), side)
    if m:
        d = marg if m.group(1).upper() == ga_u else -marg
        adj = d + float(m.group(2))
        return 'win' if adj > 0 else ('push' if abs(adj) < 1e-9 else 'loss')
    m = re.match(r'(?i)^(%s|%s)\s+to\s+win$' % (re.escape(ga_u), re.escape(gb_u)), side)
    if m:
        d = marg if m.group(1).upper() == ga_u else -marg
        # 3-way 'to win': a DRAW loses the bet (FanDuel 3-way has no push;
        # the source ledger scores draw-to-win as loss — retro-verified).
        return 'win' if d > 0 else 'loss'
    return None


# ---- bet365 deep-link resolver (Pulse Score; esoccer is in-play only) -------
# bet365 lists its esoccer (Battle / H2H GG) games essentially in-play only:
# Pulse Score returns fresh slugs for LIVE games via /live-events?sport=soccer
# (slug '/#/IP/EV…C1'), while pre-match esoccer is stale/empty in that feed.
# So the exact-game link resolves once the game is live; before kickoff the
# row falls back to the lobby link. Matches by gamer tags (the only stable
# identity across providers), never by club names.
_PS = {'at': 0.0, 'live': []}
_PS_LOCK = threading.Lock()

# bet365 NJ carries Esoccer H2H GG League + NBA2K (verified 2026-11-19 by
# rendering the live NJ site). The Esoccer quick link opens this EXACT league
# page, which lists games ~1h before kickoff — so a pre-match twin links HERE
# and the game is actually visible (lobby links left the user hunting).
# In-play: bet365 NJ shares the EV event ids with Pulse Score live slugs
# (verified: an in-site click on a live esoccer game went to /#/IP/EV…C1/,
# same ids the resolver serves), so live links are the slug on the NJ domain.
# www.bet365.com from the US bounces to a state picker and LOSES the hash —
# always link www.nj.bet365.com directly. iPhone universal links: the NJ AASA
# (6DYF8FJV35.com.bet365.bet365NJ) covers /* , so iOS opens the app natively.
# Android app is sideloaded (no Play package) — plain https links are correct.
B365 = 'https://www.nj.bet365.com'
B365_H2HGGL_LEAGUE = '/#/AC/B1/C1/D1002/E103371855/G40/N0/'
B365_NBA2K = '/#/AC/B18/C1/D1002/G1453/J97/Q1/F%5E24/'
# bet365 twins are FRESH-only: bet365 lists esoccer/NBA2K ~1h ahead, so a twin
# for a game further out opens a page where the game isn't there yet
# (user 2026-11-19: "if it's too far away game doesn't show").
B365_TWIN_WINDOW = 3600


def _bet365_live(ttl=90):
    """Pulse Score live bet365 soccer events, cached; [] on any failure."""
    key = (os.environ.get('PULSESCORE_KEY') or '').strip()
    if not key:
        return []
    now = time.time()
    if now - _PS['at'] < ttl:
        return _PS['live']
    if not _PS_LOCK.acquire(blocking=False):
        return _PS['live']
    try:
        req = urllib.request.Request(
            'https://api.pulsescore.net/api/v3/bet365/live-events?sport=soccer&limit=100',
            headers={'X-Secret': key, 'User-Agent': 'board-host/1.0'})
        d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
        _PS['live'] = [{'home': e.get('home') or '', 'away': e.get('away') or '',
                        'league': e.get('league') or '', 'slug': e.get('slug') or ''}
                       for e in (d.get('events') or [])]
        _PS['at'] = now
    except Exception:
        _PS['at'] = now    # back off this cycle; lobby links serve
    finally:
        _PS_LOCK.release()
    return _PS['live']

def _bet365_slug(home, away):
    """bet365 deep-link slug for the LIVE esoccer game matching both tags."""
    want = set(_mgm_tags(home or '') + _mgm_tags(away or ''))
    want = {w for w in want if w}
    if not want:
        return ''
    for e in _bet365_live():
        lg = (e['league'] or '').lower()
        if not ('esoccer' in lg or 'battle' in lg or 'gg league' in lg):
            continue
        have = set(_mgm_tags(e['home'])) | set(_mgm_tags(e['away']))
        if want.issubset(have) and e.get('slug'):
            return e['slug']
    return ''


# ---- bet365 live quotes (BetsAPI prematch odds = the real bet365 prices) ----
# Twin rows used to clone the source book's price. The sims books don't arb
# each other, so bet365's line/price routinely diverges by the time the user
# opens the app (2026-11-18: MIST +8.5/-125 card vs bet365 -120; ABYSS +4.5
# card vs bet365 +6.5; Under 108.5 card vs bet365 104.5). Quote the real
# bet365 numbers and stamp the row with the quote time (BetsAPI snapshots lag
# a few minutes). Cached 10 min — the board re-polls /board constantly.
_BQ = {'at': 0.0, 'up': [], 'odds': {}}
_BQ_LOCK = threading.Lock()
_BQ_TTL = 600

def _bapi(path):
    tok = (os.environ.get('BETSAPI_KEY') or '').strip()
    if not tok:
        return {}
    sep = '&' if '?' in path else '?'
    req = urllib.request.Request('https://api.betsapi.com' + path + sep + 'token=' + tok,
                                 headers={'User-Agent': 'board-host/1.0'})
    return json.loads(urllib.request.urlopen(req, timeout=12).read().decode() or '{}')

def _b365_upcoming():
    """bet365 eBasketball GG 4x5 upcoming events (sport 18 / league 25067)."""
    now = time.time()
    if now - _BQ['at'] < _BQ_TTL or not _BQ_LOCK.acquire(blocking=False):
        return _BQ['up']
    try:
        out = []
        for page in (1, 2):
            rs = _bapi('/v1/events/upcoming?sport_id=18&league_id=25067&page=%d' % page).get('results') or []
            out += rs
            if len(rs) < 50:
                break
        _BQ['up'] = out
    except Exception:
        pass
    finally:
        _BQ['at'] = now
        _BQ_LOCK.release()
    return _BQ['up']

def _b365_odds(eid, ttl=None):
    """bet365 odds for one event. ttl: per-call override of the 10-min cache —
    callers pass a SHORT ttl for games near kickoff so the twin's price is
    fresh when the user is about to bet (volume is tiny: only the last ~15
    minutes of an event's life refresh faster)."""
    now = time.time()
    c = _BQ['odds'].get(eid)
    if c and now - c[0] < (ttl or _BQ_TTL):
        return c[1]
    try:
        r = _bapi('/v1/event/odds?event_id=%s' % eid).get('results') or {}
    except Exception:
        r = {}
    _BQ['odds'][eid] = (now, r)
    return r

def _dec2am(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    if not 1.01 <= d <= 50:
        return None
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))

def _tag1(name):
    m = re.match(r'^(.*?)\s*\(([^()]+)\)$', (name or '').strip())
    return (m.group(2) or '').strip().upper() if m else ''

def _hm(add_time):
    try:
        return dt.datetime.fromtimestamp(int(add_time), dt.timezone.utc).strftime('%H:%M')
    except Exception:
        return ''

def _b365_esb_quote(o):
    """bet365 3-way refresh for an open esoccer pick: (am, 'HH:MM') or None."""
    eid = o.get('event_id')
    if not eid:
        return None
    # near kickoff: refresh the quote at most every 2.5 min instead of 10
    try:
        mins_to_start = (int(o.get('time') or 0) - time.time()) / 60.0
    except (TypeError, ValueError):
        mins_to_start = -99
    ttl = 150 if -15 <= mins_to_start <= 15 else None
    row = (_b365_odds(eid, ttl).get('1_1') or [{}])[0]
    od = row.get('home_od') if (o.get('side') or '') == (o.get('home') or '') else row.get('away_od')
    am = _dec2am(od)
    return (am, _hm(row.get('add_time'))) if am is not None else None

def _b365_bb_event(tags):
    """The bet365 GG event whose two gamer tags match (or None)."""
    want = {str(t or '').upper() for t in tags}
    want.discard('')
    if len(want) != 2:
        return None
    for e in _b365_upcoming():
        if {_tag1((e.get('home') or {}).get('name')),
                _tag1((e.get('away') or {}).get('name'))} == want:
            return e
    return None

def _b365_bb_quote(tags, market, side_tag, ou):
    """Real bet365 quote for an eBasketball GG pick; tags identify the game.

    market 'ML'|'SPREAD'|'TOTAL'. Returns {'am','line','desc','at'} or None
    when bet365 has no prematch odds for the fixture (the row then falls back
    to the source-book price with a verify flag).
    """
    ev = _b365_bb_event(tags)
    if not ev:
        return None
    # near kickoff: refresh the quote at most every 2.5 min instead of 10
    try:
        mins_to_start = (int(ev.get('time') or 0) - time.time()) / 60.0
    except (TypeError, ValueError):
        mins_to_start = -99
    r = _b365_odds(ev['id'], 150 if -15 <= mins_to_start <= 15 else None)
    mkt = (market or '').upper()
    stag = str(side_tag or '').upper()
    ht = _tag1((ev.get('home') or {}).get('name'))
    if mkt == 'ML':
        row = (r.get('18_1') or [{}])[0]
        am = _dec2am(row.get('home_od') if stag == ht else row.get('away_od'))
        return ({'am': am, 'line': None, 'desc': 'ML', 'at': _hm(row.get('add_time'))}
                if am is not None else None)
    if mkt == 'SPREAD':
        row = (r.get('18_2') or [{}])[0]
        try:
            hcp = float(str(row.get('handicap') or '').split(',')[0])
        except ValueError:
            return None
        if stag == ht:
            am, bl = _dec2am(row.get('home_od')), hcp
        else:
            am, bl = _dec2am(row.get('away_od')), -hcp
        if am is None:
            return None
        return {'am': am, 'line': bl, 'desc': '%+.1f' % bl, 'at': _hm(row.get('add_time'))}
    if mkt == 'TOTAL':
        row = (r.get('18_3') or [{}])[0]
        try:
            tot = float(str(row.get('handicap') or '').split(',')[0])
        except ValueError:
            return None
        over = str(ou or '').upper().startswith('O')
        am = _dec2am(row.get('over_od') if over else row.get('under_od'))
        if am is None:
            return None
        return {'am': am, 'line': tot,
                'desc': '%s %.1f' % ('Over' if over else 'Under', tot),
                'at': _hm(row.get('add_time'))}
    return None

def _b365_twin_note(q, card_price, card_line, howto):
    """Note fragment for a bet365 twin: live quote + moved-line/price flags."""
    if not q:
        return (' | bet365: not listed yet — it lists eBasketball only ~30 min '
                'before kickoff (about 5 games ahead). Check back closer to start; '
                'the price shown is the source book\'s. If it never appears by '
                'kickoff, bet365 pulled the game.'
                + howto)
    frag = " | bet365 now %s @ %s (quote %s UTC)" % (q['desc'], q['am'], q['at'])
    try:
        if card_line is not None and q['line'] is not None and abs(float(card_line) - float(q['line'])) > 0.01:
            frag += " — LINE MOVED (card %s)" % card_line
    except (TypeError, ValueError):
        pass
    try:
        if card_price is not None and int(card_price) != int(q['am']):
            frag += " (card %s)" % card_price
    except (TypeError, ValueError):
        pass
    return frag + howto


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
        # show games that haven't started yet, plus a ~9-min live grace so the
        # bet365 twin can still resolve the exact (in-play-only) esoccer game.
        # Stale picks past the grace expire, not display.
        st_ts = 0
        try:
            st_ts = int(o.get('time') or 0)
            if st_ts and st_ts + 540 < time.time():
                continue
        except Exception:
            pass
        # bet365 twin is FRESH-only (see B365_TWIN_WINDOW): bet365 lists these
        # games ~1h ahead; further out the game isn't on the site yet. The
        # MGM/Borgata twins below are NOT gated — Entain lists further ahead.
        b365_fresh = (not st_ts) or (st_ts - time.time() <= B365_TWIN_WINDOW)
        base = 'esb:%s:%s' % (o.get('event_id'), (o.get('side') or '').strip())
        if o.get('league') == 'h2hggl8':
            # bet365-only league ("Esoccer H2H GG League - 8 mins play"):
            # no MGM/Borgata twin row and no cds-api resolver (different
            # provider). Single bet365 row: pre-match opens bet365's OWN
            # league page (games list there ~1h before kickoff); once live,
            # the exact in-play game via the Pulse Score slug.
            if not b365_fresh:
                continue
            b365 = _bet365_slug(o.get('home'), o.get('away'))
            q = _b365_esb_quote(o)
            tgt = (B365 + b365) if b365 else (B365 + B365_H2HGGL_LEAGUE)
            bets.append({
                'key': base + '|bet365', 'kind': 'esb', 'league': o.get('league'),
                'lgName': 'bet365: Soccer > Esports > Esoccer H2H GG League - 8 mins play',
                'match': '%s v %s' % (o.get('home'), o.get('away')),
                'ga': o.get('home'), 'gb': o.get('away'),
                'market': o.get('market'), 'side': o.get('side'),
                'price': (q[0] if q else o.get('price_am')), 'stake': o.get('stake'),
                'start': _esb_iso(o.get('time')),
                'note': ('model %.1f%% EV %+.1f%%' % (100 * (o.get('model_p') or 0), 100 * _esb_note_ev(o, q))
                         + ((' | bet365 now @ %s (quote %s UTC, card %s)'
                             % (q[0], q[1], o.get('price_am'))) if q
                            else ' | bet365: no live quote — verify odds before betting')
                         + (' | bet365: opens the exact live game — check the odds match before betting.'
                            if b365 else
                            ' | bet365: opens bet365\'s Esoccer H2H GG League page — the game lists there '
                            '(games post ~1h before kickoff; if it says betting closed, check back near kickoff).')),
                'armed': True, 'book': 'bet365', 'line': '', 'legs': [],
                'web': tgt, 'app': tgt})
            continue
        # bet365 twin (user bettable there since 11-18): same pick at bet365's
        # odds (the BetsAPI feed prices these leagues off bet365). Exact-game
        # deep link once LIVE (Pulse Score esoccer is in-play only); before
        # kickoff the bet365 NJ lobby + manual match check (no verified league
        # page for the Battle/Volta comps yet). FRESH-only window applies.
        if b365_fresh:
            b365 = _bet365_slug(o.get('home'), o.get('away'))
            q = _b365_esb_quote(o)
            bets.append({
                'key': base + '|bet365', 'kind': 'esb', 'league': o.get('league'),
                'lgName': 'bet365: Soccer > Esports — find this league (e.g. Esoccer Battle - 8 mins play)',
                'match': '%s v %s' % (o.get('home'), o.get('away')),
                'ga': o.get('home'), 'gb': o.get('away'),
                'market': o.get('market'), 'side': o.get('side'),
                'price': (q[0] if q else o.get('price_am')), 'stake': o.get('stake'),
                'start': _esb_iso(o.get('time')),
                'note': ('model %.1f%% EV %+.1f%% | bet365: '
                         % (100 * (o.get('model_p') or 0), 100 * _esb_note_ev(o, q))
                         + (('now @ %s (quote %s UTC, card %s) — '
                             % (q[0], q[1], o.get('price_am'))) if q
                            else 'no live quote — verify odds. ')
                         + ('opens the exact live game — check the odds match before betting.'
                            if b365 else
                            'open bet365 (NJ) > Soccer > Esports, find the same match, check the '
                            'odds match before betting.')),
                'armed': True, 'book': 'bet365', 'line': '', 'legs': [],
                'web': (B365 + b365) if b365 else B365,
                'app': (B365 + b365) if b365 else B365})
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
    # BB (eBasketball GG 4x5): OUR OWN ridge picks vs real MGM lines — the
    # friend card only posts a couple of totals fixtures a night, these supply
    # the 2K volume (ML/Spread/Total). Open paper-ledger rows only; twins at
    # both Entain books like the esoccer rows (ids shared, price is MGM's).
    try:
        bbrows = [json.loads(x) for x in (read(os.path.join(HIST, 'esb_bb_paper_ledger.jsonl')) or '').splitlines() if x.strip()]
    except Exception:
        bbrows = []
    for o in bbrows:
        if o.get('status') != 'open':
            continue
        try:
            if int(o.get('start_ts') or 0) < time.time():
                continue   # kicked off: stays paper-only, keep off the board
        except Exception:
            pass
        # exact fixture link from the MGM id captured at pick time; fall back
        # to the league lobby if the fixture rotated off the feed
        p119 = ''
        if o.get('fid'):
            slug = '-'.join(re.sub(r'[^a-z0-9]+', ' ', (o.get('event') or '').lower()).split())
            p119 = 'events/%s-%s' % (slug, o['fid'])
        if not p119:
            p119 = _mgm_event_path(119, o.get('home') or '', o.get('away') or '',
                                  int(o.get('start_ts') or 0))
        base = 'bb:%s' % o.get('bbid')
        mkt = {'ML': 'Moneyline', 'SPREAD': 'Spread', 'TOTAL': 'Total'}.get(
            o.get('market'), o.get('market'))
        for book in ('betmgm', 'borgata'):
            lobby = NBA_BOOK_LINKS.get(book, '')
            if p119:
                web = 'https://www.nj.betmgm.com/en/sports/' + p119 if book == 'betmgm' \
                    else 'https://www.borgataonline.com/en/sports/' + p119
                app = 'https://sports.betmgm.com/en/sports/' + p119 if book == 'betmgm' \
                    else 'https://sports.borgataonline.com/en/sports/' + p119
            else:
                web = lobby
                app = NBA_BOOK_APPS.get(book, lobby)
            bets.append({
                'key': base + '|' + book, 'kind': 'nba', 'league': 'nba',
                'lgName': 'eBasketball H2H GG League - 4x5 mins — on the app: Basketball > E-Basketball',
                'match': '%s v %s' % (o.get('home'), o.get('away')),
                'ga': o.get('home'), 'gb': o.get('away'),
                'market': mkt, 'side': o.get('side'),
                'price': o.get('price_am'), 'stake': o.get('stake'),
                'start': o.get('start'),
                'note': ('our ridge: model %.1f%% EV %+.1f%% vs the MGM line | check the '
                         'odds on the slip match before betting.'
                         % (100 * (o.get('p_model') or 0), 100 * (o.get('ev') or 0))),
                'armed': True, 'book': book, 'line': '', 'legs': [],
                'web': web, 'app': app})
        # bet365 twin (bettable there since 11-18): opens bet365 NJ's NBA2K /
        # eBasketball listing (verified quick-link URL, games post ~1h ahead),
        # so the twin is FRESH-only like the esoccer twins. Price is a REAL
        # bet365 quote (BetsAPI 18_1/18_2/18_3 on the tag-matched GG event)
        # whenever bet365 posts prematch odds; otherwise the MGM clone with a
        # verify flag.
        try:
            b365_fresh = (not int(o.get('start_ts') or 0)) or (
                int(o.get('start_ts') or 0) - time.time() <= B365_TWIN_WINDOW)
        except (TypeError, ValueError):
            b365_fresh = True
        if b365_fresh:
            q = _b365_bb_quote((o.get('tag_a'), o.get('tag_b')), o.get('market'),
                               o.get('side_tag'), o.get('ou'))
            tgt = B365 + B365_NBA2K
            bets.append({
                'key': base + '|bet365', 'kind': 'nba', 'league': 'nba',
                'lgName': 'bet365: Basketball > E-Basketball (eBasketball H2H GG League - 4x5 mins)',
                'match': '%s v %s' % (o.get('home'), o.get('away')),
                'ga': o.get('home'), 'gb': o.get('away'),
                'market': mkt, 'side': o.get('side'),
                'price': (q['am'] if q else o.get('price_am')), 'stake': o.get('stake'),
                'start': o.get('start'),
                'note': ('our ridge: model %.1f%% EV %+.1f%% vs the MGM line'
                         % (100 * (o.get('p_model') or 0), 100 * _esb_note_ev(o, q))
                         + _b365_twin_note(q, o.get('price_am'), o.get('line'),
                                           ' | bet365: opens the NBA2K (eBasketball GG 4x5) '
                                           'listing — find the same match.')),
                'armed': True, 'book': 'bet365', 'line': '', 'legs': [],
                'web': tgt, 'app': tgt})
    # NBA: mirror ALL upcoming singles from the user's own board, with the
    # observer's verdict attached (armed = in the profitable >=64% band).
    # (friend card fetched once here: FD nba rows join it by key for exact
    # slip links, and the fifa section below reuses the same lookup)
    friend = _friend_card() or {}
    fby = {b.get('key'): b for b in (friend.get('bets') or [])}
    # the friend's partner profile carries his table-tennis bankroll ($1k);
    # display only — the stake scaler keeps its existing pot basis untouched.
    try:
        partner['ttBankroll'] = round(float(
            (friend.get('partner') or {}).get('ttBankroll') or 0), 2)
    except Exception:
        partner['ttBankroll'] = 0
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
    # collapse book mirrors: the Entain twins (betmgm/borgata) share a
    # platform and links -> keep the better price of the two. FanDuel gets
    # its OWN row: its prices differ and it has separate slip links, and a
    # single best-price collapse meant FD lost every tick-tie and never
    # reached the board at all.
    best = {}
    fd_row = {}
    for c, start in nba_cands:
        try:
            dec = 1 + (float(c.get('price')) / 100.0 if float(c.get('price')) > 0
                       else 100.0 / abs(float(c.get('price'))))
        except (TypeError, ValueError):
            dec = 0
        sig = (c.get('match'), (c.get('side') or '').strip().upper(), start.strftime('%Y%m%dT%H%M'))
        if (c.get('book') or '').strip().lower() == 'fanduel':
            if sig not in fd_row or dec > fd_row[sig][0]:
                fd_row[sig] = (dec, c, start)
            continue
        if sig not in best or dec > best[sig][0]:
            best[sig] = (dec, c, start)
    for dec, c, start in list(best.values()) + list(fd_row.values()):
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
        # display names: the friend card's fdName carries the full club names
        # ("Oklahoma City Thunder (GENERAL) @ Boston Celtics (FRENZY)") while
        # the scored card keeps only the tags ("GENERAL v FRENZY") — without
        # this join every 2K totals row shows anonymous tags (2026-11-18).
        frow = fby.get(c.get('key')) or {}
        fdn = (frow.get('fdName') or '').split(' @ ')
        dn_a = fdn[0].strip() if len(fdn) > 1 else ''
        dn_b = fdn[1].strip() if len(fdn) > 1 else ''
        disp = ('%s v %s' % (dn_a, dn_b)) if (dn_a and dn_b) else (c.get('match') or '')
        epath = _mgm_event_path(119, mparts[0],
                                mparts[1] if len(mparts) > 1 else '',
                                start.timestamp()) if nb in ('betmgm', 'borgata') else ''
        links = _mgm_links(epath, nb)
        # FanDuel: join the friend card by key for the exact one-click slip
        # links (the scored card drops fdName/web/app/slipIds); fall back to
        # the plain lobby links when the friend row rotated off.
        web_link = links[0] if links else NBA_BOOK_LINKS.get(nb, '')
        app_link = links[1] if links else NBA_BOOK_APPS.get(nb, '')
        if nb == 'fanduel':
            f = fby.get(c.get('key'))
            if f:
                web_link = f.get('web') or web_link
                app_link = f.get('app') or f.get('web') or app_link
        bets.append({
            'key': c.get('key'), 'kind': 'nba', 'league': 'nba',
            'lgName': ('eBasketball H2H GG League - 4x5 mins — on the app: Basketball > E-Basketball'
                       if nb in ('betmgm', 'borgata')
                       else 'FanDuel: Basketball > E-Basketball (eBasketball H2H GG)'),
            'match': disp, 'ga': disp.split(' v ')[0],
            'gb': disp.split(' v ')[1] if ' v ' in disp else '',
            'market': c.get('market'), 'side': c.get('side'),
            'price': c.get('price'), 'stake': kelly_stake if rec else 0,
            'start': start.isoformat(),
            'note': 'observer: model %.1f%% EV %s%% -> %s%s' % (
                float(c.get('model_win') or 0), c.get('ev'),
                'BET (profitable band)' if rec else 'skip (below 64%% band)',
                '' if not rec else ' | qKelly $%s' % ('%.0f' % kelly_stake)),
            'armed': rec, 'why': '' if rec else 'observer model: below the 64%% band',
            'book': nb,
            'web': web_link, 'app': app_link,
            'line': '', 'legs': []})
        if rec:
            # bet365 twin (bettable there since 11-18): the card's price is the
            # source book's. Quote bet365's REAL price/line (BetsAPI 18_x on
            # the tag-matched GG event) when it posts prematch odds; otherwise
            # keep the card price with a verify flag.
            mparts2 = (c.get('match') or ' v ').split(' v ')
            mtags = [_tag1(p) or p.strip().split()[0] if p.strip() else ''
                     for p in mparts2[:2]]
            mktu = (c.get('market') or '').strip().lower()
            q = stag = ou = card_line = None
            if mktu == 'moneyline':
                stag = _tag1(c.get('side')) or next((t for t in mtags if t and t.upper() in (c.get('side') or '').upper()), None)
                q = _b365_bb_quote(mtags, 'ML', stag, None)
            elif mktu == 'spread':
                msp = re.match(r'^(.*?)\s+([+-][\d.]+)$', (c.get('side') or '').strip())
                if msp:
                    card_line = float(msp.group(2))
                    stag = _tag1(msp.group(1)) or next((t for t in mtags if t and t.upper() in msp.group(1).upper()), None)
                    q = _b365_bb_quote(mtags, 'SPREAD', stag, None)
            elif mktu == 'total':
                mtot = re.match(r'^(Over|Under)\s+([\d.]+)$', (c.get('side') or '').strip(), re.I)
                if mtot:
                    ou = 'O' if mtot.group(1).lower() == 'over' else 'U'
                    card_line = float(mtot.group(2))
                    q = _b365_bb_quote(mtags, 'TOTAL', None, ou)
            bev = _b365_bb_event(mtags)
            if bev:
                bnm = '%s v %s' % ((bev.get('home') or {}).get('name') or '',
                                   (bev.get('away') or {}).get('name') or '')
            else:
                bnm = ''
            bdisp = bnm if ' v ' in bnm else disp
            # EV on the twin must match the price shown: recompute at bet365's
            # REAL quoted price when it differs from the card (observer ev is
            # percent units, priced at the source book's odds).
            _evq = (_ev_at(float(c.get('model_win') or 0) / 100.0, q['am'])
                    if q else None)
            _evpct = (100.0 * _evq) if _evq is not None else float(c.get('ev') or 0)
            bets.append({
                'key': c.get('key') + '|bet365', 'kind': 'nba', 'league': 'nba',
                'lgName': 'bet365: Basketball — find the same game (odds may differ)',
                'match': bdisp, 'ga': bdisp.split(' v ')[0],
                'gb': bdisp.split(' v ')[1] if ' v ' in bdisp else '',
                'market': c.get('market'), 'side': c.get('side'),
                'price': (q['am'] if q else c.get('price')), 'stake': kelly_stake if rec else 0,
                'start': start.isoformat(),
                'note': 'observer: model %.1f%% EV %.1f%% -> BET (profitable band)'
                        % (float(c.get('model_win') or 0), _evpct)
                        + _b365_twin_note(q, c.get('price'), card_line,
                                          ' | bet365: opens the NBA2K (eBasketball GG 4x5) '
                                          'listing — find the same match.'),
                'armed': True, 'book': 'bet365',
                'web': B365 + B365_NBA2K, 'app': B365 + B365_NBA2K,
                'line': '', 'legs': []})
    # FIFA (esoccer) singles: observer-scored rows joined to the friend's live
    # card for the one-click FanDuel slip links, club names and tip time.
    # Each posted key is appended to fifa_board_ledger.jsonl so /graded can
    # settle it against the H2H feed results later.
    led_path = os.path.join(HIST, 'fifa_board_ledger.jsonl')
    try:
        seen_led = {json.loads(x).get('key') for x in (read(led_path) or '').splitlines() if x.strip()}
    except Exception:
        seen_led = set()
    new_led = []
    for c in card.get('card', []):
        if c.get('league') != 'fifa' or not c.get('bet'):
            continue
        f = fby.get(c.get('key'))
        if not f or not f.get('start'):
            continue  # friend's card rotated: nothing to link -> off the board
        try:
            # ~9-min live grace so the bet365 twin can still resolve the exact
            # (in-play-only) esoccer game; stale picks past it drop off.
            if dt.datetime.fromisoformat(f['start']) < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=9):
                continue  # kicked off + grace elapsed
        except Exception:
            pass
        bets.append({
            'key': c.get('key'), 'kind': 'fifa', 'league': 'fifa',
            'lgName': 'FanDuel: Soccer > Esports — esoccer clubs (H2H)',
            'match': '%s v %s' % (f.get('ga') or '?', f.get('gb') or '?'),
            'ga': f.get('ga'), 'gb': f.get('gb'),
            'fdName': f.get('fdName'),
            'market': c.get('market'), 'side': c.get('side'),
            'price': c.get('price'), 'stake': c.get('stake'),
            'start': f.get('start'),
            'note': ('observer: model %.1f%% EV %+.1f%% -> BET (profitable band)'
                     ' | FanDuel: the button loads the exact selection - check'
                     ' the odds on the slip match before betting.'
                     % (float(c.get('model_win') or 0), float(c.get('ev') or 0))),
            'armed': True, 'book': 'fanduel', 'line': '', 'legs': [],
            'web': f.get('web') or '', 'app': f.get('app') or f.get('web') or ''})
        # bet365 twin (bettable there since 11-18): same pick, exact-game deep
        # link once LIVE, bet365 NJ lobby before kickoff. FRESH-only window
        # (bet365 lists esoccer ~1h ahead; further out the game isn't there).
        fresh_b365 = True
        try:
            if (dt.datetime.fromisoformat(f['start'])
                    - dt.datetime.now(dt.timezone.utc)).total_seconds() > B365_TWIN_WINDOW:
                fresh_b365 = False
        except Exception:
            pass
        if fresh_b365:
            b365 = _bet365_slug(f.get('ga'), f.get('gb'))
            tgt = (B365 + b365) if b365 else B365
            bets.append({
                'key': c.get('key') + '|bet365', 'kind': 'fifa', 'league': 'fifa',
                'lgName': 'bet365: Soccer > Esports — find the same match (odds may differ)',
                'match': '%s v %s' % (f.get('ga') or '?', f.get('gb') or '?'),
                'ga': f.get('ga'), 'gb': f.get('gb'),
                'fdName': f.get('fdName'),
                'market': c.get('market'), 'side': c.get('side'),
                'price': c.get('price'), 'stake': c.get('stake'),
                'start': f.get('start'),
                'note': ('observer: model %.1f%% EV %+.1f%% -> BET (profitable band) | bet365: '
                         % (float(c.get('model_win') or 0), float(c.get('ev') or 0))
                         + ('opens the exact live game — check the odds match before betting.'
                            if b365 else
                            'open Soccer > Esports, find the same match, check the odds '
                            'match before betting.')),
                'armed': True, 'book': 'bet365', 'line': '', 'legs': [],
                'web': tgt, 'app': tgt})
        if c.get('key') not in seen_led:
            seen_led.add(c.get('key'))
            new_led.append(json.dumps({
                'key': c.get('key'), 'ga': f.get('ga'), 'gb': f.get('gb'),
                'fdName': f.get('fdName'), 'start': f.get('start'),
                'market': c.get('market'), 'side': c.get('side'),
                'price': c.get('price'), 'stake': c.get('stake'),
                'posted': dt.datetime.now(dt.timezone.utc).isoformat()}))
    if new_led:
        try:
            with open(led_path, 'a', encoding='utf-8') as fh:
                fh.write('\n'.join(new_led) + '\n')
        except Exception:
            pass
    # TABLE TENNIS (TT Elite Series, Poland — FanDuel only): mirror ALL live
    # rows from the friend's card, singles AND parlays, with his one-click FD
    # slip links. Settles off HIS board: graded_rows() emits his settled
    # `mine` results for the same keys (pnl recomputed board-side from the
    # user's own stake/price). A pick he never places has no result source —
    # it ages out open (known gap, 2026-11-20).
    tt_scored = {c.get('key'): c for c in card.get('card', [])
                 if c.get('league') == 'tt'}
    for f in (friend.get('bets') or []):
        if (f.get('league') or '') != 'tt':
            continue
        try:
            fst = dt.datetime.fromisoformat((f.get('start') or '').replace('Z', '+00:00'))
        except Exception:
            continue
        # FanDuel TT Elite props are IN-PLAY ONLY (no pre-match market) and a
        # best-of-5 match runs ~15-25 min. Keep the row visible through the
        # whole live window so the user can live-bet it — a 9-min grace dropped
        # rows mid-match (user 2026-11-24: "you can only live bet the props so
        # they disappear when I try to bet them"). 40 min covers the longest
        # possible match + a comfortable margin.
        if fst < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40):
            continue  # well past the live window
        is_parlay = (f.get('kind') == 'parlay')
        s = None if is_parlay else tt_scored.get(f.get('key'))
        if s:
            note = ('observer: model %.1f%% EV %+.1f%% — NBA-trained extrapolation, '
                    'NOT TT-calibrated | FanDuel: the button loads the exact selection - '
                    'check the odds on the slip match before betting.'
                    % (float(s.get('model_win') or 0), float(s.get('ev') or 0)))
        else:
            note = ('FanDuel: the button loads the exact selection(s) - check the odds '
                    'on the slip match the card before betting.')
        bets.append({
            'key': f.get('key'),
            'kind': 'parlay' if is_parlay else 'tt', 'league': 'tt',
            'lgName': 'FanDuel: Table Tennis — TT Elite Series (Poland)',
            'match': (f.get('fdName')
                      or ('%s v %s' % (f.get('ga') or '?', f.get('gb') or '?'))),
            'ga': f.get('ga') or '', 'gb': f.get('gb') or '',
            'market': f.get('market'), 'side': f.get('side'),
            'price': f.get('price'), 'stake': f.get('stake'),
            'start': f.get('start'), 'note': note,
            'armed': bool(f.get('armed', True)), 'book': 'fanduel',
            'line': (f.get('line') if f.get('line') is not None else ''),
            'legs': f.get('legs') or [],
            'web': f.get('web') or '', 'app': f.get('app') or f.get('web') or ''})
        # DraftKings twin (same TT Elite match — DK's TT menu is the deepest
        # of the US books). No deep-link resolver: DK's site/app is
        # bot-walled from datacenter IPs, so the button opens the DK lobby
        # and the note tells the user to find the same match. The shown
        # price/EV are FanDuel's (verify-flag note per the twin rule — never
        # clone a source price silently); grading settles off the SAME
        # friend-ledger result via the |draftkings twin key in graded_rows().
        dk_note = ('DraftKings equivalent of the FanDuel pick: same TT Elite match — '
                   'open Table Tennis in the DK app/site and find this match. '
                   'VERIFY the odds on the DK slip: the price/EV shown are FanDuel\'s '
                   'and DK\'s line can differ.'
                   + (' DK parlays: build the same legs on one ticket (Parlay line).'
                      if is_parlay else ''))
        bets.append({
            'key': f.get('key') + '|draftkings',
            'kind': 'parlay' if is_parlay else 'tt', 'league': 'tt',
            'lgName': 'DraftKings: Table Tennis — TT Elite Series (Poland)',
            'match': (f.get('fdName')
                      or ('%s v %s' % (f.get('ga') or '?', f.get('gb') or '?'))),
            'ga': f.get('ga') or '', 'gb': f.get('gb') or '',
            'market': f.get('market'), 'side': f.get('side'),
            'price': f.get('price'), 'stake': f.get('stake'),
            'start': f.get('start'), 'note': dk_note,
            'armed': bool(f.get('armed', True)), 'book': 'draftkings',
            'line': (f.get('line') if f.get('line') is not None else ''),
            'legs': f.get('legs') or [],
            'web': 'https://sportsbook.draftkings.com/',
            'app': 'https://sportsbook.draftkings.com/'})
    # best EV first so the top of the card is the brain's strongest conviction.
    # Notes without a parseable 'EV x%' (e.g. TT rows the observer never
    # scored) previously crashed this float(); park them at the bottom.
    def _evkey(b):
        try:
            return -float((b.get('note') or '').split('EV ')[-1].split('%')[0] or -99)
        except ValueError:
            return 99.0
    bets.sort(key=_evkey)
    # Dedupe visually-identical rows: same book + market + side + start + tag
    # pair = the SAME bet under two keys. The friend card posts one fixture
    # as separate book-mirror rows (plain + __BORGATA) whose scored 'match'
    # strings can differ (tag order), so the sig collapse above misses them
    # and the board showed the same bet365 twin twice under two keys
    # (2026-11-19). Keep the better price; tie keeps the higher-EV row.
    def _amdec(p):
        try:
            p = float(p)
            return 1 + (p / 100.0 if p > 0 else 100.0 / abs(p))
        except (TypeError, ValueError):
            return 0.0
    _seen = {}
    _out = []
    for b in bets:
        tags = tuple(sorted(filter(None, (_tag1(b.get('ga')), _tag1(b.get('gb'))))))
        if not tags or len(tags) < 2:
            tags = (str(b.get('ga') or '').upper(), str(b.get('gb') or '').upper())
        sig = (str(b.get('book') or '').lower(), str(b.get('market') or '').upper(),
               str(b.get('side') or '').upper(), str(b.get('start') or ''), tags)
        if sig in _seen:
            if _amdec(b.get('price')) > _amdec(_seen[sig].get('price')):
                _out[_out.index(_seen[sig])] = b
                _seen[sig] = b
            continue
        _seen[sig] = b
        _out.append(b)
    bets = _out
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
                         'lg': ((x.get('competition') or {}).get('name') or {}).get('value') or '',
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

def _mgm_event(sport, home, away, start_ts, window=300):
    """the book fixture matching both gamer tags + start time, or None.

    The gamer tags are the only stable identity across providers: the same
    virtual match is labeled with different club names by BetsAPI/bet365
    ("PSG (RossFCDK) v Bayern (DangerDim77)") and by the Entain books
    ("Paris (RossFCDK) - Bayern Munchen (DangerDim77)") — match on tags,
    then use the book's own name for display."""
    want = _mgm_tags(home or '') + _mgm_tags(away or '')
    want = [w for w in want if w]
    if not want:
        return None
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
    return best


def _mgm_event_path(sport, home, away, start_ts, window=300):
    """'events/<slug>-<id>' for the fixture matching both tags + start time"""
    best = _mgm_event(sport, home, away, start_ts, window)
    if not best:
        return ''
    slug = '-'.join(re.sub(r'[^a-z0-9]+', ' ', best['name'].lower()).split())
    return 'events/%s-%s' % (slug, best['id'])


def _mgm_side_book(side, book_name):
    """the pick's name as the BOOK writes it: the half of the book fixture
    name carrying the pick's gamer tag ("Paris (RossFCDK)" for a side
    logged as "PSG (RossFCDK)"). Empty when the side carries no tag."""
    t = re.findall(r'\(([^)]+)\)', str(side or ''))
    if not t:
        return ''
    want = re.sub(r'[^A-Z0-9]', '', t[0].upper())
    for half in re.split(r'\s+-\s+', str(book_name or '')):
        ht = re.findall(r'\(([^)]+)\)', half)
        if ht and re.sub(r'[^A-Z0-9]', '', ht[0].upper()) == want:
            return half.strip()
    return ''

def _mgm_links(path, book):
    """(web, app) links for a book given a shared event path, else None"""
    if not path or book not in ('betmgm', 'borgata'):
        return None
    www = 'https://www.nj.betmgm.com/en/sports/' if book == 'betmgm' \
        else 'https://www.borgataonline.com/en/sports/'
    app = 'https://sports.betmgm.com/en/sports/' if book == 'betmgm' \
        else 'https://sports.borgataonline.com/en/sports/'
    return (www + path, app + path)


def _iso_ts(iso):
    """unix ts from an ISO datetime string, 0 on any failure"""
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _enrich_sims(text, sport, home_fn, away_fn, ts_fn):
    """Ledger JSONL text with BetMGM/Borgata slip links added to open rows.

    Sims are paper bets priced at the Entain books; the board's Sims tab
    shows the open ones. Attach the same exact-match deep links the
    To-place rows get (both books share ids, one resolution serves both).
    Rows that don't resolve (book doesn't carry the game, or bet365-only
    leagues which the Entain resolver can't see) serve without links and
    the front-end shows no buttons. Never rewrites the ledger on disk:
    this runs on the /board response only.
    """
    out, memo = [], {}
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except Exception:
            out.append(line)
            continue
        if sport and (o.get('status') or '').lower() == 'open':
            sig = (home_fn(o), away_fn(o), ts_fn(o))
            if sig not in memo:
                memo[sig] = _mgm_event(sport, sig[0] or '', sig[1] or '',
                                       sig[2])
            fx = memo[sig]
            path = ('events/%s-%s' % (
                '-'.join(re.sub(r'[^a-z0-9]+', ' ', fx['name'].lower()).split()),
                fx['id'])) if fx else ''
            links = {}
            for book in ('betmgm', 'borgata'):
                L = _mgm_links(path, book)
                if L:
                    links[book] = {'web': L[0], 'app': L[1]}
            if links:
                o['links'] = links
                # the books label the SAME virtual match with their own club
                # names ("Paris (RossFCDK) - Bayern Munchen (DangerDim77)",
                # league "E-soccer - Battle Champions League 2x4 Minutes") —
                # show those on the linked row so what the user reads on the
                # board is exactly what the book shows after the tap.
                o['bookName'] = fx['name']
                o['bookLg'] = fx.get('lg') or ''
                sb = _mgm_side_book(o.get('side'), fx['name'])
                if sb:
                    o['sideBook'] = sb
        out.append(json.dumps(o))
    return '\n'.join(out)


def graded_rows():
    out = []
    def rows(path):
        try:
            return [json.loads(x) for x in (read(path) or '').splitlines() if x.strip()]
        except Exception:
            return []
    for o in rows(os.path.join(HIST, 'esb_paper_ledger.jsonl')):
        if o.get('status') in ('win', 'loss', 'push'):
            # the board posts each esb pick as TWO rows (betmgm + borgata) plus
            # a bet365 twin, so grade all suffixed keys; the un-placed twins
            # simply never show.
            base = 'esb:%s:%s' % (o.get('event_id'), (o.get('side') or '').strip())
            # h2hggl8 rows are posted bet365-only: grade just that suffixed key
            books = ('bet365',) if o.get('league') == 'h2hggl8' else ('bet365', 'betmgm', 'borgata')
            for book in books:
                out.append({'key': base + '|' + book,
                            'result': o['status'], 'pnl': o.get('pnl')})
    # our own eBasketball GG ridge picks (posted as betmgm/borgata twins plus a
    # bet365 twin)
    for o in rows(os.path.join(HIST, 'esb_bb_paper_ledger.jsonl')):
        if o.get('status') == 'settled' and o.get('result') in ('win', 'loss', 'push'):
            base = 'bb:%s' % o.get('bbid')
            for book in ('bet365', 'betmgm', 'borgata'):
                out.append({'key': base + '|' + book,
                            'result': o['result'], 'pnl': o.get('pnl')})
    for name in ('model_sb_ledger.jsonl', 'model_ledger.jsonl'):
        for o in rows(os.path.join(HERE, name)):
            if o.get('result') in ('win', 'loss', 'push') and o.get('key'):
                out.append({'key': o['key'], 'result': o['result'], 'pnl': o.get('pnl')})
                # the board also posts a bet365 twin of every NBA single
                out.append({'key': o['key'] + '|bet365', 'result': o['result'],
                            'pnl': o.get('pnl')})
    # FIFA picks posted to the board: grade against the H2H feed results
    # ($HIST/history/real_games.json, written by the scorer's feed fetcher).
    try:
        games = json.load(open(os.path.join(HIST, 'history', 'real_games.json'))).get('by_date', {})
    except Exception:
        games = {}
    for o in rows(os.path.join(HIST, 'fifa_board_ledger.jsonl')):
        res = _grade_fifa(o, games)
        if res:
            # pnl is computed board-side from the placement's own price/stake
            out.append({'key': o.get('key'), 'result': res, 'pnl': None})
            # plus the bet365 twin of the same pick
            out.append({'key': o.get('key') + '|bet365', 'result': res, 'pnl': None})
    # TABLE TENNIS: settles off the friend's own board — his `mine` rows
    # carry his settled results for the same TT_* keys (win/loss on his
    # ledger). pnl stays None so the board recomputes from the user's own
    # stake/price. Picks he never places carry no result (known gap).
    friend = _friend_card() or {}
    for k, v in (friend.get('mine') or {}).items():
        if str(v.get('league') or '').lower() != 'tt':
            continue
        res = str(v.get('result') or '').lower()
        if res in ('win', 'loss', 'push'):
            out.append({'key': k, 'result': res, 'pnl': None})
            # DraftKings TT twins share the friend-ledger result (the pick's
            # outcome is book-independent); pnl stays None so the board
            # recomputes it from the user's own DK stake/price.
            out.append({'key': k + '|draftkings', 'result': res, 'pnl': None})
    # manual/backfill grades (seeded from the repo; survives /var/data wipes).
    # Orphaned board placements whose pipeline ledger row was wiped by a
    # deploy settle from here — pnl stays None so the board recomputes it
    # from the user's own stake/price (see board settle_pass).
    seen = {r['key'] for r in out}
    for o in rows(os.path.join(HIST, 'manual_graded.jsonl')):
        if o.get('key') and o.get('result') in ('win', 'loss', 'push') \
                and o['key'] not in seen:
            out.append({'key': o['key'], 'result': o['result'], 'pnl': None})
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
                               # Sims tab feeds: model paper ledgers (graded +
                               # open rows) for the board's Sims tab. Open
                               # rows are enriched with BetMGM/Borgata slip
                               # links (Entain resolver; bet365-only leagues
                               # carry none).
                               'esb_ledger': _enrich_sims(
                                   read_tail(os.path.join(HIST, 'esb_paper_ledger.jsonl'), 400), 108,
                                   lambda o: o.get('home'), lambda o: o.get('away'),
                                   lambda o: int(o.get('time') or 0)),
                               'model_sb_ledger': _enrich_sims(
                                   read_tail(os.path.join(HERE, 'model_sb_ledger.jsonl'), 400), 119,
                                   lambda o: (o.get('match') or ' v ').split(' v ')[0],
                                   lambda o: (o.get('match') or ' v ').split(' v ')[-1],
                                   lambda o: _iso_ts(o.get('start'))),
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
    ledgers_restore()
    threading.Thread(target=_ledger_backup_loop, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get('PORT', '10000'))
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
