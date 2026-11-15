#!/usr/bin/env python
"""
h2hggl_recorder.py — fully-automatic real-results recorder for the placed-bet grader.

DISCOVERY: h2hggl.com is a JS SPA, but it talks to a plain JSON API at
    https://api-h2h.cdn-hudstats.com/v1/schedule/{sport}?date={ISO+TZ}
where sport = {"ebasketball":"nba","esoccer":"fifa","eamericanfootball":"nfl"}.
Unlike web_extract (which needs an agent), a plain urllib request CAN hit this
JSON endpoint. So the ENTIRE real-results recording is scriptable: fetch per-day,
merge into a growing JSON index, and the placed_grader.py grades against it.

    python h2hggl_recorder.py           # fetch all days, update history/real_games.json
    python h2hggl_recorder.py --days 7  # last 7 days only

Output (READ-ONLY vs board; writes only under history/):
    history/real_games.json  merged alias-pair -> {teams, score, dates:[]} index
    history/feed_log.jsonl   one line per fetch (date, games, http)
Runs on a cron (every 30 min) WITHOUT any agent/model — it just needs the machine
online. Immune to the model-provider / wifi issues that kill agent crons.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("SCORER_HISTORY_DIR", os.path.join(HERE, "history"))
INDEX = os.path.join(OUT_DIR, "real_games.json")
UPCOMING = os.path.join(OUT_DIR, "upcoming_fixtures.json")
LOG = os.path.join(OUT_DIR, "feed_log.jsonl")
API = "https://api-h2h.cdn-hudstats.com/v1/schedule/nba"
API_BASE = "https://api-h2h.cdn-hudstats.com"
LEAGUES = ["nba", "fifa"]   # both have real results on the h2hggl API
DEFAULT_DAYS = 30  # cover the full placed-book span
TZ = "-05:00"      # US Eastern, DST already fell back (Nov 2026)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://h2hggl.com/", "Origin": "https://h2hggl.com/"}

os.makedirs(OUT_DIR, exist_ok=True)


def fetch_one(date_iso, sport="nba"):
    url = "%s/v1/schedule/%s?date=%sT00:00:00%s" % (API_BASE, sport, date_iso, TZ)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_day(day_json):
    """h2hggl schedule JSON is a LIST of matches with exact fields:
    participantAName / participantBName / teamAScore / teamBScore /
    teamAName / teamBName / matchStatus. Keep only finished (MATCH_ENDED)."""
    games = []
    if not isinstance(day_json, list):
        return games
    for m in day_json:
        if not isinstance(m, dict):
            continue
        a = m.get("participantAName") or m.get("participantA")
        b = m.get("participantBName") or m.get("participantB")
        sa = m.get("teamAScore"); sb = m.get("teamBScore")
        ta = m.get("teamAName"); tb = m.get("teamBName")
        # keep only finished matches with a real (non-zero) score
        if not (isinstance(sa, int) and isinstance(sb, int)):
            continue
        if sa == 0 and sb == 0:
            continue
        if a and b:
            games.append({"aliases": sorted([str(a).upper(), str(b).upper()]),
                          "score": [sa, sb], "teams": (str(ta or ""), str(tb or "")),
                          "status": m.get("matchStatus"), "start": m.get("startDate")})
    return games


def main():
    days = DEFAULT_DAYS
    if "--days" in sys.argv:
        try:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        except (ValueError, IndexError):
            pass
    today = dt.date.today()
    dates = [today - dt.timedelta(days=i) for i in range(days)]

    index = {}
    if os.path.exists(INDEX):
        index = json.load(open(INDEX, encoding="utf-8"))

    # ---- upcoming fixtures (for the engine trader's live picks) ----
    # Same schedule JSON contains not-yet-ended matches: aliases + brands + start.
    # Store them under their schedule date so the engine can find the next
    # fixture for a card pair. Rewritten fresh each run (fixtures go stale).
    today_iso = today.isoformat()
    tomorrow_iso = (today + dt.timedelta(days=1)).isoformat()
    upcoming = {"by_date": {today_iso: [], tomorrow_iso: []}}
    for iso, sport in ((today_iso, "nba"), (today_iso, "fifa"),
                       (tomorrow_iso, "nba"), (tomorrow_iso, "fifa")):
        try:
            day_json = fetch_one(iso, sport)
        except Exception:
            continue
        for m in (day_json if isinstance(day_json, list) else []):
            if not isinstance(m, dict):
                continue
            if m.get("matchStatus") == "MATCH_ENDED":
                continue
            a = m.get("participantAName"); b = m.get("participantBName")
            if not (a and b):
                continue
            upcoming["by_date"][iso].append({
                "aliases": [str(a).upper(), str(b).upper()],
                "teams": [str(m.get("teamAName") or ""), str(m.get("teamBName") or "")],
                "start": m.get("startDate"), "status": m.get("matchStatus"),
                "league": sport,
            })
        time.sleep(1.0)
    json.dump(upcoming, open(UPCOMING, "w", encoding="utf-8"), indent=1)

    new = 0
    log = []
    for d in dates:
        iso = d.isoformat()
        for sport in LEAGUES:
            try:
                day_json = fetch_one(iso, sport)
                games = parse_day(day_json)
            except urllib.error.HTTPError as e:
                log.append({"date": iso, "sport": sport, "http": e.code, "games": 0, "err": "HTTP %s" % e.code})
                time.sleep(1.5)
                continue
            except Exception as e:
                log.append({"date": iso, "sport": sport, "games": 0, "err": str(e)})
                time.sleep(1.5)
                continue
            for g in games:
                g["league"] = sport          # tag the sport so graders can filter
                # Keep EVERY game with its start time — pairs meet multiple times/day
                # with different scores; the grader disambiguates by nearest start.
                days_bucket = index.setdefault("by_date", {}).setdefault(iso, [])
                sig = (tuple(g["aliases"]), g["start"], tuple(g["score"]))
                if sig not in {tuple((tuple(x["aliases"]), x.get("start"), tuple(x["score"]))) for x in days_bucket}:
                    days_bucket.append(g)
                    new += 1
            log.append({"date": iso, "sport": sport, "games": len(games),
                        "http": "ok"})
            time.sleep(1.2)  # be polite to the API (the 429 came from rapid probes)

    json.dump(index, open(INDEX, "w", encoding="utf-8"), indent=1)
    with open(LOG, "a", encoding="utf-8") as fh:
        for e in log:
            fh.write(json.dumps(e) + "\n")

    n_games = sum(len(v) for v in index.get("by_date", {}).values())
    print(json.dumps({"fetched_days": len(dates), "new_games": new,
                      "total_game_rows": n_games, "days_indexed": len(index.get("by_date", {})),
                      "latest": [e for e in log[-3:]]}, indent=1))


if __name__ == "__main__":
    main()
