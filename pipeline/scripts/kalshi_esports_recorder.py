"""kalshi_esports_recorder.py — standalone Kalshi esports data collector.

Records, from Kalshi's PUBLIC API (no auth, no board, no h2hggl):
  1. LIVE QUOTES   — every open eBasketball/eSoccer market's bid/ask, per sweep
  2. SETTLEMENTS   — every finalized market's result (the answer key), once
  3. FIXTURES      — event tickers embed game time; titles carry participants

Files (under picks-analysis/kalshi-bot/kalshi_history/):
  quotes.jsonl    append: {ts, ticker, bid, ask}   (open markets, each sweep)
  settled.jsonl   append-once per ticker: {ticker, event, title, result, close}
  sweep_log.jsonl one line per sweep: {ts, n_open, n_new_settled}

Run: python kalshi_esports_recorder.py        (used by 2h cron, no_agent)
"""
import json, os, time, datetime as dt, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(os.environ.get('SCORER_HISTORY_DIR', os.path.join(HERE, 'kalshi_history')), 'kalshi_history')
os.makedirs(HIST, exist_ok=True)
QUOTES = os.path.join(HIST, 'quotes.jsonl')
SETTLED = os.path.join(HIST, 'settled.jsonl')
SWEEP = os.path.join(HIST, 'sweep_log.jsonl')

SERIES = ['KXEBASKETBALLGAME', 'KXESOCCERGAME', 'KXEFOOTBALLGAME']
BASE = 'https://api.elections.kalshi.com/trade-api/v2/markets'

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
      'Referer': 'https://kalshi.com/'}


def fetch(url, tries=3):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=25).read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(8); continue
            raise
        except Exception:
            time.sleep(4)
    return {}


def load_settled_keys():
    keys = set()
    if os.path.exists(SETTLED):
        for l in open(SETTLED, encoding='utf-8'):
            try:
                keys.add(json.loads(l)['ticker'])
            except Exception:
                pass
    return keys


def main():
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.isoformat(timespec='seconds')
    seen = load_settled_keys()
    n_open = 0
    n_new = 0

    with open(QUOTES, 'a', encoding='utf-8') as qf, open(SETTLED, 'a', encoding='utf-8') as sf:
        # 1) open markets -> live quotes
        for s in SERIES:
            d = fetch(f'{BASE}?series_ticker={s}&status=open&limit=200')
            for m in d.get('markets', []):
                n_open += 1
                qf.write(json.dumps({'ts': ts, 'ticker': m['ticker'],
                                     'event': m.get('event_ticker'),
                                     'title': m.get('title', '')[:60],
                                     'bid': m.get('yes_bid_dollars'),
                                     'ask': m.get('yes_ask_dollars'),
                                     'close': (m.get('close_time') or '')[:16]}) + '\n')
            time.sleep(0.7)

        # 2) settled markets -> settlements (answer key), new ones only
        for s in SERIES:
            cursor = ''
            for _ in range(3):  # up to 600 most-recent settled per series
                url = f'{BASE}?series_ticker={s}&status=settled&limit=200'
                if cursor:
                    url += f'&cursor={cursor}'
                d = fetch(url)
                ms = d.get('markets', [])
                if not ms:
                    break
                for m in ms:
                    if m['ticker'] in seen:
                        continue
                    seen.add(m['ticker'])
                    n_new += 1
                    sf.write(json.dumps({'ticker': m['ticker'],
                                         'event': m.get('event_ticker'),
                                         'title': m.get('title', '')[:60],
                                         'result': m.get('result'),
                                         'close': (m.get('close_time') or '')[:16],
                                         'captured': ts}) + '\n')
                cursor = d.get('cursor', '')
                if not cursor:
                    break
                time.sleep(0.7)

    with open(SWEEP, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': ts, 'open_markets': n_open, 'new_settled': n_new}) + '\n')

    print(f'Kalshi esports sweep @ {ts}: {n_open} open quoted, {n_new} new settlements recorded.')


if __name__ == '__main__':
    main()