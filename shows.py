"""Every Latin roster show in New York, Los Angeles and Miami, from the Ticketmaster Discovery API.

Roster only: a show is listed only when an artist on the roster (data/latin.json) is on the bill.

    python3 shows.py        sweep Ticketmaster, rebuild index.html

One sweep per city pulls every music event within the city's radius for the next year, 200 at
a time, and keeps a show when the event or any act on its bill is classed as Latin or a Latin
style (reggaeton, regional Mexican, salsa, bachata...: LATIN_WORDS). Ticketmaster files many
Latin acts under Pop or Hip-Hop at the event level but Latin at the artist level, so both count. data/shows_seen.json records when each event
was first seen (ids and times only); that is how the page knows what was just announced.
The first sweep is a baseline: nothing on it counts as just announced.

Outputs: index.html (page.html, the Shows This Week page adapted for Latin, with the data inside)
and shows.json (the same rows, for anything else). Room sizes: capacity.py.
The key comes from TICKETMASTER_API_KEY (environment, .env, or the GitHub secret).
"""
import datetime, json, os, sys
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ticketmaster as tm
from latin import CITIES, NOT_A_SHOW, where, norm, day, clock, onsale_note, STAMP

LATIN = "KnvZfZ7vAJ6"   # Ticketmaster's Music > Latin genre (all its subgenres: pop, urbano, regional, rock...)
AHEAD = 365             # days ahead to list
NEW_DAYS = 30           # Just announced: first seen in the last 30 days
SOON_DAYS = 14          # Upcoming: the next two weeks
KEEP_SEEN_DAYS = 60
SEEN = os.path.join(HERE, "data", "shows_seen.json")
CITY_NAME = {k: n for k, n, *_ in CITIES}


LATIN_WORDS = ("latin", "reggaeton", "regional mex", "mexican", "salsa", "bachata", "merengue", "cumbia", "banda",
               "norte", "mariachi", "tropical", "urbano", "tejano", "flamenco", "tango", "bossa", "samba", "brazil",
               "dembow", "corrido", "ranchera", "bolero", "son cubano", "vallenato", "afro-cuban", "latino")


def latin(e):
    """True when the event, or any act on its bill, is classed as Latin or a Latin style."""
    cls = list(e.get("classifications") or [])
    for a in e.get("_embedded", {}).get("attractions", []):
        cls += a.get("classifications") or []
    for c in cls:
        for k in ("genre", "subGenre", "type", "subType"):
            n = ((c.get(k) or {}).get("name") or "").lower()
            if n and any(w in n for w in LATIN_WORDS):
                return True
    return False


def sweep(c, key, lat, lon, radius):
    out = {}

    def window(a, b):
        params = dict(segmentId=",".join(tm.SEGMENTS), geoPoint=tm.geohash(lat, lon), radius=radius, unit="miles", size=200,
                      sort="date,asc", locale="*", startDateTime=f"{a}T00:00:00Z", endDateTime=f"{b}T23:59:59Z")
        first = c.get("events.json", page=0, **params)
        total = first.get("page", {}).get("totalElements", 0)
        if total > 1000 and a < b:
            mid = a + (b - a) // 2
            window(a, mid)
            window(mid + datetime.timedelta(days=1), b)
            return
        pages = [first] + [c.get("events.json", page=p, **params) for p in range(1, min(first.get("page", {}).get("totalPages", 1), 5))]
        for res in pages:
            for e in res.get("_embedded", {}).get("events", []):
                v = (e.get("_embedded", {}).get("venues") or [{}])[0]
                if NOT_A_SHOW.search(e.get("name") or "") or "redirect" in (v.get("name") or "").lower():
                    continue
                if where(v) != key or not latin(e):
                    continue
                out[e["id"]] = row(e, v, key)

    today = datetime.date.today()
    window(today - datetime.timedelta(days=1), today + datetime.timedelta(days=AHEAD))
    return out


def row(e, v, key):
    r = tm.record(e)
    acts = r["acts"] or [r["name"]]
    p = sorted(r["prices"], key=lambda p: p.get("type") != "standard")
    return {"id": r["tmId"], "date": r["date"], "time": r["time"], "event": r["name"],
            "headliner": acts[0], "support": acts[1:], "venue": r["venue"],
            "venueCity": ", ".join(x for x in (r["city"], r["state"]) if x), "city": key,
            "subgenre": ((e.get("classifications") or [{}])[0].get("subGenre") or {}).get("name"),
            "tickets": r["url"], "onsale": r["onsale"], "presale": r["presale"],
            "price": [p[0].get("min"), p[0].get("max")] if p else None,
            "status": r["status"] if r["status"] in ("cancelled", "postponed", "rescheduled") else None}


def main():
    key = tm.api_key()
    if not key:
        sys.exit("TICKETMASTER_API_KEY is not set (environment, .env, or the GitHub secret)")
    os.makedirs(tm.OUT, exist_ok=True)
    c = tm.Client(key)
    shows = {}
    try:
        for k, _, lat, lon, radius in CITIES:
            shows.update(sweep(c, k, lat, lon, radius))
    except tm.OutOfQuota as err:
        sys.exit(f"Stopped: {err}. Nothing was rebuilt.")
    finally:
        c.save()

    # Ticketmaster lists some nights twice (a resale or platinum copy): keep the shortest name.
    best = {}
    for s in sorted(shows.values(), key=lambda s: (bool(s["status"]), len(s["event"] or ""), s["id"])):
        best.setdefault((norm(s["headliner"]), s["date"], s["city"]), s)
    rows = sorted(best.values(), key=lambda s: (s["date"], s["time"] or "99", s["headliner"]))

    # Roster dates (latin.py's feed) that Ticketmaster doesn't class as Latin still belong here.
    fp = os.path.join(HERE, "latin", "feed.json")
    if os.path.exists(fp):
        have = {(norm(s["headliner"]), s["date"]) for s in rows} | {(norm(a), s["date"]) for s in rows for a in s["support"]}
        cname = {n: k for k, n, *_ in CITIES}
        for f in json.load(open(fp))["shows"]:
            k = cname.get(f.get("city"))
            if f.get("group") != "roster" or not k or not f.get("date") or (norm(f["artist"]), f["date"]) in have:
                continue
            rid = (f.get("tickets") or "").rstrip("/").rsplit("/", 1)[-1] or f"{norm(f['artist'])}-{f['date']}"
            rows.append({"id": rid, "date": f["date"], "time": f.get("time"), "event": f["artist"], "headliner": f["artist"],
                         "support": [], "venue": f.get("venue"), "venueCity": f.get("venueCity"), "city": k, "subgenre": None,
                         "tickets": f.get("tickets"), "onsale": f.get("onsale"), "presale": f.get("presale"),
                         "price": None, "status": None})
        rows.sort(key=lambda s: (s["date"], s["time"] or "99", s["headliner"]))

    # Roster only: a show is listed when a roster artist is on the bill, nobody else's.
    lp = os.path.join(HERE, "data", "latin.json")
    rset = {norm(a.get("display") or a["name"]) for a in json.load(open(lp))["artists"] if a.get("group") == "roster"} if os.path.exists(lp) else set()
    rows = [s for s in rows if any(norm(a) in rset for a in [s["headliner"], *s["support"]])]

    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime(STAMP)
    first_run = not os.path.exists(SEEN)
    seen = {} if first_run else json.load(open(SEEN))
    for s in rows:
        seen.setdefault(s["id"], "baseline" if first_run else stamp)
        s["firstSeen"] = seen[s["id"]]
        s["note"] = onsale_note(s, stamp)
    ids, cutoff = {s["id"] for s in rows}, (now - datetime.timedelta(days=KEEP_SEEN_DAYS)).strftime(STAMP)
    seen = {k: v for k, v in seen.items() if k in ids or (v != "baseline" and v >= cutoff)}
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    json.dump(dict(sorted(seen.items())), open(SEEN, "w"), indent=0)

    today = str(datetime.date.today())
    rows = [s for s in rows if s["date"] and s["date"] >= today]
    json.dump({"updated": stamp, "source": "Ticketmaster Discovery API, genre Latin", "shows": rows},
              open(os.path.join(HERE, "shows.json"), "w"), ensure_ascii=False, indent=0)
    write_page(rows, now)
    print(f"{len(rows)} shows: " + ", ".join(f"{n} {sum(s['city'] == k for s in rows)}" for k, n in CITY_NAME.items())
          + f"; {c.calls} API calls")


# Ticketmaster's venue names carry sponsors and suffixes; the page uses the room's plain name.
VENUE_ALIAS = {
    "James L Knight Center": "James L. Knight Center",
    "The Fillmore Miami Beach at Jackie Gleason Theater": "The Fillmore Miami Beach",
    "Watsco Center at the University of Miami": "Watsco Center",
    "The Novo by Microsoft": "The Novo",
    "Irving Plaza Powered By Verizon 5G": "Irving Plaza",
    "DigAlert Grove of Anaheim": "Grove of Anaheim",
    "House of Blues Anaheim": "House Of Blues Anaheim",
}
PAGE_KEY = {"ny": "nyc", "la": "la", "mia": "mia"}
WINDOW_DAYS = 365  # Upcoming: every roster date in the next year


def venue_name(v):
    v = " ".join((v or "").split())
    return VENUE_ALIAS.get(v, v)


def write_page(rows, now):
    """Fill page.html (the Shows This Week page, adapted) with every city's shows and write index.html."""
    import capacity
    roster = []
    lp = os.path.join(HERE, "data", "latin.json")
    if os.path.exists(lp):
        roster = [a.get("display") or a["name"] for a in json.load(open(lp))["artists"] if a.get("group") == "roster"]
    rnorm = {norm(r): r for r in roster}
    today = datetime.date.today()
    week = [str(today), str(today + datetime.timedelta(days=WINDOW_DAYS))]
    cutoff = (now - datetime.timedelta(days=NEW_DAYS)).strftime(STAMP)
    stamp_day = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    doc = {"week": week, "cities": []}
    for k, name, *_ in CITIES:
        key = PAGE_KEY[k]
        shows, announced = [], []
        for s in rows:
            if s["city"] != k:
                continue
            venue = venue_name(s["venue"])
            acts = [s["headliner"], *s["support"]]
            x = {"id": (s["id"] if key == "nyc" else f"{key}:{s['id']}"), "date": s["date"], "time": s["time"],
                 "headliner": s["headliner"], "support": s["support"], "venue": venue, "age": None,
                 "ticketUrl": s["tickets"],
                 "clients": [{"artist": rnorm[norm(a)], "agents": [], "service": []} for a in acts if norm(a) in rnorm],
                 "tm": {kk: vv for kk, vv in {"url": s["tickets"], "onsale": s["onsale"], "presale": s["presale"],
                        "price": (s["price"] + ["USD"]) if s["price"] else None, "status": s["status"]}.items() if vv}}
            c = capacity.cap(key, venue)
            if c:
                x["cap"] = c
            if s["firstSeen"] != "baseline":
                x["firstSeen"] = s["firstSeen"]
            if s["date"] <= week[1]:
                shows.append(x)
            elif x.get("firstSeen", "") >= cutoff:
                announced.append(x)
        doc["cities"].append({"key": key, "name": name, "checked": stamp_day,
                              "sources": {"Ticketmaster": stamp_day}, "shows": shows, "announced": announced})
    page = open(os.path.join(HERE, "page.html")).read()
    a, b = "/*DATA*/", "/*END*/"
    i, j = page.index(a) + len(a), page.index(b)
    body = page[:i] + json.dumps(doc, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + page[j:]
    open(os.path.join(HERE, "index.html"), "w").write(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="description" content="Every Latin roster show in New York, Los Angeles and Miami, from Ticketmaster. Updated every morning.">\n'
        '<style>body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style>\n'
        + body + "\n</html>\n")


if __name__ == "__main__":
    main()
