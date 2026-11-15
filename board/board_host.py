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

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


# ---------------- db ----------------

def db():
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
    if not c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        u, p = os.environ.get('FIRST_ADMIN_USERNAME'), os.environ.get('FIRST_ADMIN_PASSWORD')
        if u and p:
            create_user(c, u, p, 'admin')
            print('bootstrapped first admin:', u, '- REMOVE the env vars after first login')
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
    if amount is None:
        return None
    try:
        p = float(price if price is not None else -110)
        dec = 1 + (p / 100 if p > 0 else 100 / abs(p))
    except (TypeError, ValueError):
        return None
    return round(amount * (dec - 1), 2) if result == 'win' else -float(amount)


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
    for r in rows:
        try:
            pl = json.loads(r['payload'])
        except Exception:
            continue
        key = str(pl.get('srcKey') or pl.get('key') or '')
        if key not in verd:
            continue
        res, pnl = verd[key]
        if pnl is None:
            pnl = _pnl_for(res, pl.get('amount'), pl.get('price'))
        pl['result'] = res
        if pnl is not None:
            pl['pnl'] = pnl
        c.execute("UPDATE placements SET payload=? WHERE rowid=?",
                  (json.dumps(pl), r['rowid']))
    c.commit()
    c.close()


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
        if path == '/' or path == '/index.html':
            try:
                body = open(INDEX, encoding='utf-8').read().encode()
            except Exception:
                body = b'<h1>board index.html missing</h1>'
            self._send(200, body)
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
            act, key = body.get('act'), body.get('key')
            if act in ('place', 'skip') and key:
                payload = json.dumps({'status': 'placed' if act == 'place' else 'skipped',
                                      'key': key,
                                      **{k: body.get(k) for k in ('amount', 'price', 'limited', 'match', 'league', 'start')}})
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
            self._send(200, b'{"ok":true}', 'application/json')
            return
        self._send(404, b'not found')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    init_db()
    threading.Thread(target=_settle_loop, daemon=True).start()
    print('board host on', PORT, '| card:', CARD_SOURCE or 'NOT SET',
          '| settle:', SETTLE_SOURCE or 'NOT SET')
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
