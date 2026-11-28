#!/usr/bin/env python
"""board_host.py — multi-user hosted picks board (for the user + a few friends).

Architecture: the BRAIN (cloud agent) serves the live card at CARD_SOURCE
(e.g. https://<cloud>/data). This app is the DISPLAY + ACCOUNTS layer:
  - scrypt-hashed passwords (unique salt per user), sessions stored as hashes,
    httpOnly SameSite=Lax cookies, expiry, logout, login rate-limit + lockout
  - no public registration: the admin creates accounts via /admin (first admin
    bootstrapped from FIRST_ADMIN_USERNAME/PASSWORD only while users table is
    empty — remove after first login)
  - roles: admin (user management, everything) / user (view card, place & track
    their OWN bets, their own record + bankroll inputs)
  - per-user placements + settings in SQLite (this app's own DB; it never
    writes the brain's data)

Env: DB_PATH, CARD_SOURCE, SETTLE_SOURCE (a URL returning the graded ledger
      as {key: {result: win|loss|push, pnl?: number}} or a list of such rows),
      SETTLE_EVERY (seconds, default 120), PORT, FIRST_ADMIN_USERNAME,
      FIRST_ADMIN_PASSWORD, SESSION_SECRET (optional extra hash pepper).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hosted_board.db'))
CARD_SOURCE = os.environ.get('CARD_SOURCE', '')
SETTLE_SOURCE = os.environ.get('SETTLE_SOURCE', '')
SETTLE_EVERY = int(os.environ.get('SETTLE_EVERY', '120'))
PORT = int(os.environ.get('PORT', '8793'))
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
SESSION_TTL = 14 * 86400
LOCKOUT = {}          # username -> [fails, locked_until]
_failed = {}
_failed_mu = threading.Lock()

# Render free tier wipes /var/data on every deploy AND every spin-down/wake —
# the board's SQLite DB does not survive, which silently destroyed the users'
# placement/record history. Persistence shim: keep the DB as a file on a git
# branch (NOT the production branch, so pushes never trigger deploys). Restore
# on boot when the local DB is fresh; push a debounced snapshot after writes.
# Dormant (no-op) unless GH_BACKUP_TOKEN is set in the service env.
GH_TOKEN   = (os.environ.get('GH_BACKUP_TOKEN') or '').strip()
GH_REPO    = (os.environ.get('GH_BACKUP_REPO') or 'bino47/picks-stack').strip()
GH_BRANCH  = (os.environ.get('GH_BACKUP_BRANCH') or 'board-db').strip()
GH_FILE    = 'board.db.bak'

_dirty = threading.Event()


def _gh(path, method='GET', body=None, timeout=30):
    req = urllib.request.Request('https://api.github.com' + path,
        headers={'Authorization': 'token ' + GH_TOKEN,
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'},
        method=method, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or '{}')


def backup_restore():
    """Boot: if the local DB is fresh (no placements) and a backup exists on
    the backup branch, restore it. Returns True when a restore happened."""
    if not GH_TOKEN:
        return False
    try:
        st, d = _gh(f'/repos/{GH_REPO}/contents/{GH_FILE}?ref={GH_BRANCH}')
        raw = base64.b64decode(d.get('content') or '')
        if len(raw) < 512:
            return False
        c = db()
        n = c.execute("SELECT COUNT(*) AS n FROM placements").fetchone()['n']
        c.close()
        if n:
            return False
        tmp = DB_PATH + '.restore'
        with open(tmp, 'wb') as f:
            f.write(raw)
        t = sqlite3.connect(tmp)          # sanity: restored file must be a
        t.execute("SELECT COUNT(*) FROM users")   # valid sqlite DB
        t.close()
        os.replace(tmp, DB_PATH)
        print('board db: restored from', GH_REPO, GH_BRANCH)
        return True
    except Exception as e:
        print('board db: restore skipped:', e)
        return False


def mark_dirty():
    if GH_TOKEN:
        _dirty.set()


def _backup_loop():
    """Debounced pusher: one snapshot push per dirty window, at most every
    60s. Never raises into the request path."""
    while True:
        _dirty.wait()
        time.sleep(60)          # debounce bursts (settle pass touches many rows)
        _dirty.clear()
        try:
            tmp = DB_PATH + '.snapshot'
            s = sqlite3.connect(DB_PATH)
            t = sqlite3.connect(tmp)
            s.backup(t)         # consistent snapshot even mid-write
            t.close(); s.close()
            with open(tmp, 'rb') as f:
                content = base64.b64encode(f.read()).decode()
            sha = None
            try:
                st, d = _gh(f'/repos/{GH_REPO}/contents/{GH_FILE}?ref={GH_BRANCH}')
                sha = d.get('sha')
            except Exception:
                sha = None      # file or branch may not exist yet
            body = {'message': 'board db backup', 'content': content,
                    'branch': GH_BRANCH}
            if sha:
                body['sha'] = sha
            try:
                _gh(f'/repos/{GH_REPO}/contents/{GH_FILE}', 'PUT', body)
            except urllib.error.HTTPError as e:
                if e.code == 422 and not sha:      # branch missing: create it
                    st, m = _gh(f'/repos/{GH_REPO}/git/ref/heads/main')
                    _gh(f'/repos/{GH_REPO}/git/refs', 'POST',
                        {'ref': 'refs/heads/' + GH_BRANCH,
                         'sha': m.get('object', {}).get('sha')})
                    _gh(f'/repos/{GH_REPO}/contents/{GH_FILE}', 'PUT', body)
                else:
                    raise
            os.unlink(tmp)
        except Exception as e:
            print('board db: backup failed:', e)   # retried on next dirty


SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


# ---------------- db ----------------

def db():
    d = os.path.dirname(DB_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
      pwhash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
      created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(
      token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
      expires REAL NOT NULL, created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS placements(
      user_id INTEGER NOT NULL, pkey TEXT NOT NULL,
      payload TEXT NOT NULL, created TEXT NOT NULL,
      UNIQUE(user_id, pkey));
    CREATE TABLE IF NOT EXISTS settings(
      user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
    """)
    c.commit()
    # HARD RESET on every boot: the host idles/wakes (free tier) and each wake is a
    # fresh session — clear all placed bets so the board starts clean (user request).
    n = c.execute("SELECT COUNT(*) FROM placements").fetchone()[0]
    if n:
        c.execute("DELETE FROM placements")
        c.commit()
        print(f'boot reset: cleared {n} placement(s)')
    c.close()


def ensure_env_users():
    """Create FIRST_ADMIN + ADD_USERS users. Runs AFTER backup_restore(): on the
    free tier the boot restore overwrites the DB file, so any user created
    before it would be wiped. Idempotent: skips usernames that already exist."""
    c = db()
    if not c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        u, p = os.environ.get('FIRST_ADMIN_USERNAME'), os.environ.get('FIRST_ADMIN_PASSWORD')
        if u and p:
            create_user(c, u, p, 'admin')
            print('bootstrapped first admin:', u, '- REMOVE the env vars after first login')
    # one-time account creation from env (ADD_USERS="user:pass[;u2:p2[:role]]"),
    # set via Render API — FREE PLAN HAS NO DISK: the sqlite DB is ephemeral and
    # wiped on every deploy/sleep-wake, so this var must STAY SET to recreate
    # accounts on each boot. passwords never live in the repo.
    for spec in (os.environ.get('ADD_USERS') or '').split(';'):
        spec = spec.strip()
        if not spec or ':' not in spec:
            continue
        parts = spec.split(':', 2)
        u, p = parts[0], parts[1]
        role = parts[2] if len(parts) > 2 and parts[2] else 'user'
        if u and p and not c.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
            create_user(c, u, p, role)
            print('ADD_USERS: created', u, 'role', role)
        elif u and len(parts) > 2 and parts[2]:
            # Explicit role on an EXISTING user: enforce it (promote/demote) each boot.
            # Only fires when a role IS specified — a bare "u:p" seed never demotes.
            cur = c.execute("SELECT role FROM users WHERE username=?", (u,)).fetchone()
            if cur and cur[0] != role:
                c.execute("UPDATE users SET role=? WHERE username=?", (role, u))
                c.commit()
                print('ADD_USERS: set role', role, 'for', u)
    c.close()


def _repair_pnl():
    """One-shot boot repair: recompute pnl of already-graded placements from
    their OWN stake+price. Rows graded before the settle-pass fix inherited
    the pipeline feed's ledger pnl (~$25 paper stakes), not the user's actual
    stake. Idempotent — _pnl_for of the same inputs is deterministic."""
    c = db()
    rows = c.execute("""SELECT p.rowid, p.payload FROM placements p
                        WHERE json_extract(p.payload, '$.result') IN ('win','loss')""").fetchall()
    fixed = 0
    for r in rows:
        try:
            pl = json.loads(r['payload'])
        except Exception:
            continue
        try:
            own = _pnl_for(pl.get('result'), pl.get('stake', pl.get('amount')), pl.get('price'))
            if own is not None and pl.get('pnl') != own:
                pl['pnl'] = own
                c.execute("UPDATE placements SET payload=? WHERE rowid=?",
                          (json.dumps(pl), r['rowid']))
                fixed += 1
        except Exception:
            continue          # never let one bad row kill the boot
    if fixed:
        c.commit()
        print(f'pnl repair: recomputed {fixed} graded row(s) from own stake/price')
        try:
            mark_dirty()
        except Exception:
            pass
    c.close()


def hash_pw(pw, salt):
    dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt),
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return base64.b64encode(dk).decode()


def create_user(c, username, password, role):
    salt = secrets.token_hex(16)
    c.execute("INSERT INTO users(username, pwhash, salt, role, created) VALUES(?,?,?,?,?)",
              (username, hash_pw(password, salt), salt, role,
               time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    c.commit()


# ---------------- auth ----------------

def check_login(username, password):
    now = time.time()
    with _failed_mu:
        st = _failed.get(username)
        if st and st[1] > now:
            return None, 'locked — try later'
    c = db()
    row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    c.close()
    if not row or not hmac.compare_digest(row['pwhash'], hash_pw(password, row['salt'])):
        with _failed_mu:
            fails = (_failed.get(username, [0, 0]))[0] + 1
            _failed[username] = [fails, now + 600 if fails >= 5 else 0]
        return None, 'bad username or password'
    with _failed_mu:
        _failed.pop(username, None)
    return row, None


def make_session(user_id):
    token = secrets.token_urlsafe(32)
    c = db()
    c.execute("INSERT INTO sessions(token_hash, user_id, expires, created) VALUES(?,?,?,?)",
              (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + SESSION_TTL,
               time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    c.commit()
    c.close()
    return token


def user_from_request(handler):
    cookie = handler.headers.get('Cookie') or ''
    token = None
    for part in cookie.split(';'):
        k, _, v = part.strip().partition('=')
        if k == 'pbsession':
            token = v
    if not token:
        return None
    c = db()
    row = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                       WHERE s.token_hash=? AND s.expires > ?""",
                    (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    c.close()
    return row


def destroy_session(handler):
    cookie = handler.headers.get('Cookie') or ''
    for part in cookie.split(';'):
        k, _, v = part.strip().partition('=')
        if k == 'pbsession' and v:
            c = db()
            c.execute("DELETE FROM sessions WHERE token_hash=?",
                      (hashlib.sha256(v.encode()).hexdigest(),))
            c.commit()
            c.close()


# ---------------- card source ----------------

def _card_bet(key):
    """Look up one card row by key, for enriching a placement with the
    display fields the page renders (pick/stake/book/market/start...)."""
    try:
        for b in fetch_card().get('bets') or []:
            if b.get('key') == key:
                return b
    except Exception:
        pass
    return None


_card_cache = {"at": 0.0, "data": None}


def fetch_card():
    if not CARD_SOURCE:
        return {"bets": [], "title": "CARD_SOURCE not set", "record": {}}
    now = time.time()
    if _card_cache["data"] is not None and now - _card_cache["at"] < 60:
        return _card_cache["data"]
    try:
        req = urllib.request.Request(CARD_SOURCE, headers={"User-Agent": "board-host/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        _card_cache.update({"at": now, "data": data})
        return data
    except Exception as e:
        if _card_cache["data"] is not None:
            return _card_cache["data"]
        return {"bets": [], "title": "card source unreachable", "error": str(e), "record": {}}


# ---------------- settlement ----------------

def _norm_settle(raw):
    """Accept {key: {result,...}} or [{key, result,...}] and return
    {key: (result, pnl_or_None)} for results in win/loss/push."""
    out = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((r.get('key'), r) for r in raw if isinstance(r, dict))
    else:
        return out
    for k, v in items:
        if not k or not isinstance(v, dict):
            continue
        res = (v.get('result') or v.get('status') or '').lower()
        if res not in ('win', 'loss', 'push'):
            continue
        out[str(k)] = (res, v.get('pnl') if isinstance(v.get('pnl'), (int, float)) else None)
    return out


def _pnl_for(result, amount, price):
    if result == 'push':
        return 0.0
    # stakes/prices arrive as strings from the page ('46.62', '-110', '+100')
    # and older rows store them that way — coerce, and return None (never
    # raise) on non-numeric data so one bad row can't kill a boot or a settle
    # pass.
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return None
    if not amt > 0:
        return None
    try:
        p = float(price) if price is not None else -110.0
    except (TypeError, ValueError):
        return None
    dec = 1 + (p / 100 if p > 0 else 100 / abs(p))
    return round(amt * (dec - 1), 2) if result == 'win' else round(-amt, 2)


def settle_pass():
    if not SETTLE_SOURCE:
        return
    try:
        req = urllib.request.Request(SETTLE_SOURCE, headers={"User-Agent": "board-host/1.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    except Exception:
        return
    verd = _norm_settle(raw)
    if not verd:
        return
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    c = db()
    rows = c.execute("""SELECT p.rowid, p.payload FROM placements p
                        WHERE json_extract(p.payload, '$.status') = 'placed'""").fetchall()
    updated = 0
    for r in rows:
        try:
            pl = json.loads(r['payload'])
        except Exception:
            continue
        key = str(pl.get('srcKey') or pl.get('key') or '')
        if key not in verd:
            continue
        res, pnl = verd[key]
        # P&L must reflect THIS user's stake/price, not the feed's own ledger
        # (the pipeline grades its paper rows at ~$25 qKelly stakes; copying
        # that pnl onto a user's $46.62 row reported a -25.00 loss on a 46.62
        # risk — bets looked wrong). Recompute whenever the placement carries
        # its own stake and price; trust the feed pnl only as a fallback.
        own = _pnl_for(res, pl.get('stake', pl.get('amount')), pl.get('price'))
        if own is not None:
            pnl = own
        elif pnl is None:
            pnl = _pnl_for(res, pl.get('amount'), pl.get('price'))
        pl['result'] = res
        if pnl is not None:
            pl['pnl'] = pnl
        c.execute("UPDATE placements SET payload=? WHERE rowid=?",
                  (json.dumps(pl), r['rowid']))
        updated += 1
    c.commit()
    c.close()
    if updated:
        mark_dirty()


def _settle_loop():
    while True:
        try:
            settle_pass()
        except Exception:
            pass
        time.sleep(max(15, SETTLE_EVERY))


def user_record(uid):
    """Per-user graded record from their own placements."""
    c = db()
    rows = c.execute("SELECT payload FROM placements WHERE user_id=?", (uid,)).fetchall()
    c.close()
    rec = {'w': 0, 'l': 0, 'p': 0, 'pnl': 0.0, 'pending': 0}
    for r in rows:
        try:
            pl = json.loads(r['payload'])
        except Exception:
            continue
        if pl.get('status') == 'skipped':
            continue
        res = pl.get('result')
        if res == 'win':
            rec['w'] += 1
            rec['pnl'] += pl.get('pnl', 0)
        elif res == 'loss':
            rec['l'] += 1
            rec['pnl'] += pl.get('pnl', 0)
        elif res == 'push':
            rec['p'] += 1
        else:
            rec['pending'] += 1
    rec['pnl'] = round(rec['pnl'], 2)
    return rec


def compute_record(uid):
    """Feed for the page's Record tab (GET /record): summary buckets
    (today / 7 days / all time, all-time split byLeague), by-day table
    and the raw rows, all from this user's own placements."""
    c = db()
    rows = c.execute("""SELECT payload, created FROM placements
                        WHERE user_id=? ORDER BY created DESC""", (uid,)).fetchall()
    c.close()
    pls = []
    for r in rows:
        try:
            pl = json.loads(r['payload'])
            pl['_created'] = r['created']
            pls.append(pl)
        except Exception:
            continue

    def bucket(items):
        b = {'n': 0, 'w': 0, 'l': 0, 'p': 0, 'pnl': 0.0, 'staked': 0.0, 'open': 0}
        for pl in items:
            b['n'] += 1
            res = pl.get('result')
            pnl = pl.get('pnl')
            if not isinstance(pnl, (int, float)):
                pnl = _pnl_for(res, pl.get('stake', pl.get('amount')), pl.get('price'))
            if res == 'win':
                b['w'] += 1
                b['pnl'] += pnl or 0
            elif res == 'loss':
                b['l'] += 1
                b['pnl'] += pnl or 0
            elif res == 'push':
                b['p'] += 1
            else:
                b['open'] += 1
            if res in ('win', 'loss', 'push'):
                try:
                    b['staked'] += float(pl.get('stake', pl.get('amount')) or 0)
                except (TypeError, ValueError):
                    pass
        graded = b['w'] + b['l'] + b['p']
        b['roi'] = round(b['pnl'] / b['staked'] * 100, 1) if b['staked'] and graded else None
        b['pnl'] = round(b['pnl'], 2)
        b['staked'] = round(b['staked'], 2)
        return b

    def placed_of(items):
        return [p for p in items if p.get('status') == 'placed']

    now = time.time()
    today = time.strftime('%Y-%m-%d', time.gmtime())
    week_ago = now - 7 * 86400

    def created_ts(pl):
        try:
            return time.mktime(time.strptime(pl['_created'], '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
        except Exception:
            return 0

    all_placed = placed_of(pls)
    today_placed = [p for p in all_placed if time.strftime('%Y-%m-%d', time.gmtime(created_ts(p))) == today]
    week_placed = [p for p in all_placed if created_ts(p) >= week_ago]
    by_league = {}
    # dynamic: bucket every league present in this user's placements (hardcoded
    # nba/nfl/fifa silently dropped esoccer leagues -> sums didn't add up)
    for lg in sorted({p.get('league') for p in all_placed if p.get('league')}):
        sub = [p for p in all_placed if p.get('league') == lg]
        by_league[lg] = {'placed': bucket(sub)}
    days = []
    seen = {}
    for p in all_placed:
        d = time.strftime('%Y-%m-%d', time.gmtime(created_ts(p)))
        seen.setdefault(d, []).append(p)
    for d in sorted(seen, reverse=True)[:30]:
        days.append({'day': d, 'placed': bucket(seen[d])})
    rec_rows = []
    for p in pls:
        st = p.get('status')
        if st not in ('placed', 'skipped'):
            continue
        rec_rows.append({
            'key': p.get('key'), 'status': st, 'kind': p.get('kind'),
            'placed': {'pick': p.get('pick'), 'price': p.get('price'),
                       'stake': p.get('stake', p.get('amount')), 'limited': p.get('limited')},
            'book': p.get('book'), 'league': p.get('league'), 'match': p.get('match'),
            'market': p.get('market'), 'start': p.get('start'),
            'result': p.get('result'), 'score': p.get('score'), 'pnl': p.get('pnl'),
            'at': p.get('_created'),
        })
    return {'summary': {'today': {'placed': bucket(today_placed)},
                        'week': {'placed': bucket(week_placed)},
                        'all': {'placed': bucket(all_placed), 'byLeague': by_league}},
            'days': days, 'rows': rec_rows}


# ---------------- http ----------------

LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Picks board</title>
<style>body{font-family:system-ui;background:#f3f1ea;color:#16201a;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}
form{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.1);width:280px}
h1{font-size:18px;margin:0 0 14px}input{width:100%;padding:9px;margin:5px 0 12px;border:1px solid #ccc;
border-radius:7px;box-sizing:border-box}button{width:100%;padding:10px;border:0;border-radius:8px;
background:#16201a;color:#fff;font-weight:700;cursor:pointer}
.err{color:#b42318;font-size:13px;margin-bottom:10px}</style></head><body>
<form method="post" action="/login">
<h1>Picks board</h1>
__ERR__
<input name="u" placeholder="username" autocomplete="username" required>
<input name="p" type="password" placeholder="password" autocomplete="current-password" required>
<button>Sign in</button></form></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='text/html; charset=utf-8', cookies=()):
        self.send_response(code)
        for c in cookies:
            self.send_header('Set-Cookie', c)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to, cookies=()):
        self.send_response(303)
        for c in cookies:
            self.send_header('Set-Cookie', c)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    # --- pages ---
    def do_GET(self):
        user = user_from_request(self)
        path = self.path.split('?')[0]
        if path == '/login':
            err_html = ('<div class="err">%s</div>' % self._err) if hasattr(self, '_err') else ''
            self._send(200, LOGIN_PAGE.replace('__ERR__', err_html).encode())
            return
        if path == '/logout':
            destroy_session(self)
            self._redirect('/login', cookies=['pbsession=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax'])
            return
        if not user:
            self._redirect('/login')
            return
        if path == '/health':
            # public liveness probe: what the board's card feed is serving
            card = fetch_card()
            bets = card.get('bets') or []
            tots = [b for b in bets if 'total' in str(b.get('market') or '').lower()
                    and str(b.get('league') or '').lower() in ('nba', '2k')]
            self._send(200, json.dumps({
                'card_at': card.get('at') or card.get('checkedAt'),
                'live': card.get('live'), 'why': card.get('why'),
                'bets': len(bets), 'nba_totals': len(tots),
                'fd_slips': sum(1 for b in bets if 'marketId=' in str(b.get('web') or '')),
                'sample_totals': [((b.get('side') or '') + '|' + str(b.get('book'))) for b in tots[:8]],
            }).encode(), 'application/json')
            return
        if path == '/' or path == '/index.html':
            try:
                body = open(INDEX, encoding='utf-8').read().encode()
            except Exception:
                body = b'<h1>board index.html missing</h1>'
            self._send(200, body)
            return
        if path == '/record':
            self._send(200, json.dumps({'ok': True, **compute_record(user['id'])}).encode(),
                       'application/json')
            return
        if path == '/data':
            card = fetch_card()
            c = db()
            rows = c.execute("SELECT pkey, payload FROM placements WHERE user_id=?", (user['id'],)).fetchall()
            st = c.execute("SELECT payload FROM settings WHERE user_id=?", (user['id'],)).fetchone()
            c.close()
            mine = {}
            for r in rows:
                try:
                    mine[r['pkey']] = json.loads(r['payload'])
                except Exception:
                    pass
            card = dict(card)
            card['mine'] = mine
            card['settings'] = json.loads(st['payload']) if st else {}
            card['myrecord'] = user_record(user['id'])
            card['role'] = user['role']
            self._send(200, json.dumps(card).encode(), 'application/json')
            return
        if path == '/admin' and user['role'] == 'admin':
            c = db()
            users = [{'username': r['username'], 'role': r['role'], 'created': r['created']}
                     for r in c.execute("SELECT * FROM users").fetchall()]
            c.close()
            self._send(200, (json.dumps(users, indent=1)).encode(), 'application/json')
            return
        self._send(404, b'not found')

    # --- posts ---
    def do_POST(self):
        user = user_from_request(self)
        path = self.path.split('?')[0]
        if path == '/login':
            n = int(self.headers.get('Content-Length') or 0)
            q = parse_qs(self.rfile.read(n).decode() if n else '')
            row, err = check_login((q.get('u') or [''])[0], (q.get('p') or [''])[0])
            if err:
                self._send(200, LOGIN_PAGE.replace('__ERR__', '<div class="err">%s</div>' % err).encode())
                return
            tok = make_session(row['id'])
            self._redirect('/', cookies=['pbsession=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d'
                                         % (tok, SESSION_TTL)])
            return
        if not user:
            self._send(403, b'login required')
            return
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() if n else '{}')
        except Exception:
            body = {}
        if path == '/act':                       # place / skip / undo / bankroll — own rows only
            c = db()
            act, key = body.get('action') or body.get('act'), body.get('key')
            amt = None
            if act in ('place', 'skip') and key:
                # a place with no/zero amount can never settle to a real P&L
                # (one such row existed: stake=None forever) — reject it.
                if act == 'place':
                    try:
                        amt = float(body.get('amount') or 0)
                    except (TypeError, ValueError):
                        amt = 0
                    if not (amt > 0):
                        c.close()
                        self._send(400, b'{"ok":false,"error":"stake required"}',
                                   'application/json')
                        return
                # Enrich with the card row so Placed/Record render pick, book,
                # market, match, tip time — the page's placedItem() needs them.
                b = _card_bet(key) or {}
                stake = amt if act == 'place' else body.get('amount')   # numeric — strings broke _pnl_for
                try:
                    px = float(body.get('price') or b.get('price'))
                except (TypeError, ValueError):
                    px = body.get('price') or b.get('price')
                payload = json.dumps({'status': 'placed' if act == 'place' else 'skipped',
                                      'key': key,
                                      'pick': body.get('side') or b.get('side'),
                                      'stake': stake if act == 'place' else body.get('amount'),
                                      'price': px,
                                      'limited': bool(body.get('limited')),
                                      'kind': b.get('kind'), 'book': b.get('book'),
                                      'league': b.get('league'), 'match': b.get('match'),
                                      'market': b.get('market'), 'start': b.get('start'),
                                      **{k: body.get(k) for k in ('match', 'league', 'start') if body.get(k)}})
                c.execute("INSERT INTO placements(user_id, pkey, payload, created) VALUES(?,?,?,?) "
                          "ON CONFLICT(user_id, pkey) DO UPDATE SET payload=excluded.payload",
                          (user['id'], key, payload, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
            elif act == 'undo' and key:
                c.execute("DELETE FROM placements WHERE user_id=? AND pkey=?", (user['id'], key))
            elif act == 'settings':
                c.execute("INSERT INTO settings(user_id, payload) VALUES(?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
                          (user['id'], json.dumps(body.get('settings') or {})))
            elif act == 'set_password' and body.get('current') and body.get('new'):
                row = c.execute("SELECT * FROM users WHERE id=?", (user['id'],)).fetchone()
                ok = row and hmac.compare_digest(row['pwhash'], hash_pw(body['current'], row['salt']))
                if not ok or len(body['new']) < 8:
                    c.close()
                    self._send(400, b'{"ok":false,"error":"bad current password or new password too short"}',
                               'application/json')
                    return
                salt = secrets.token_hex(16)
                c.execute("UPDATE users SET pwhash=?, salt=? WHERE id=?",
                          (hash_pw(body['new'], salt), salt, user['id']))
            elif act == 'admin_create_user' and user['role'] == 'admin':
                try:
                    create_user(c, body.get('username'), body.get('password'), 'user')
                except Exception:
                    c.close()
                    self._send(400, b'user exists / bad input')
                    return
            c.commit()
            c.close()
            mark_dirty()
            self._send(200, b'{"ok":true}', 'application/json')
            return
        self._send(404, b'not found')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    init_db()
    backup_restore()          # free tier: /var/data starts empty on every boot
    ensure_env_users()        # AFTER restore: env users would be wiped by it
    _repair_pnl()             # after restore: fix graded rows whose pnl came from the feed's ledger
    threading.Thread(target=_settle_loop, daemon=True).start()
    if GH_TOKEN:
        threading.Thread(target=_backup_loop, daemon=True).start()
    print('board host on', PORT, '| card:', CARD_SOURCE or 'NOT SET',
          '| settle:', SETTLE_SOURCE or 'NOT SET',
          '| db backup:', 'on' if GH_TOKEN else 'OFF (set GH_BACKUP_TOKEN)')
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
