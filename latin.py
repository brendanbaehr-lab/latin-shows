"""THE·TEAM Latin roster and pursuits: every Ticketmaster date in New York, Los Angeles and Miami.

Runs with no person in the loop (GitHub Actions, .github/workflows/latin.yml) or by hand:

    python3 latin.py                         refresh: look up every artist, rebuild latin/
    python3 latin.py list                    the list, with each artist's Ticketmaster id
    python3 latin.py add "Name"              add a pursuit (an artist THE·TEAM is chasing, not signed)
    python3 latin.py add "Name" --roster     add a signed artist
    python3 latin.py add "Name" --tm ID      ...pinned to a Ticketmaster attraction id
    python3 latin.py sign "Name"             a pursuit has signed: move it to the roster
    python3 latin.py remove "Name"
    python3 latin.py pin "Name" ID           fix a wrong Ticketmaster match ("none": not on Ticketmaster; "auto": search again)
    python3 latin.py init roster.txt         start the list from a file, one roster name a line

The list lives in data/latin.enc, encrypted with the client key (CLIENTS_KEY, or .clients-key
locally), so no data file in this public repo carries client or pursuit names in plain text.
Each artist: name (as typed), display (Ticketmaster's spelling), group (roster or pursuit),
tm (attraction id; "none" = not on Ticketmaster), noMatch (date a name search last failed;
retried weekly), tracked (date of the first lookup; dates already on sale then are not news).
data/latin_seen.json records when each event was first seen (ids and times only), which is how
the report knows what is newly announced.

Outputs, all built and published (the main Shows site is public since 2026-11-22):
  latin/index.html    the page
  latin/feed.json     the same data for ChatGPT (a Custom GPT action reads it; latin/openapi.json)
  latin/feed.txt      the same again as plain text, for pasting or for ChatGPT's browser
Sources: the Ticketmaster Discovery API, by artist (every date, any city), plus THE·TEAM's client
sheet where it has been decrypted locally or on Actions (data/clients_raw_<city>.json, NYC and LA).
Dates sold only through AXS, DICE, Eventbrite or a venue's own site are missed; the page says so.
"""
import datetime, html, json, math, os, re, sys, unicodedata
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LIST = os.path.join(HERE, "data", "latin.enc")
SEEN = os.path.join(HERE, "data", "latin_seen.json")
OUT = os.path.join(HERE, "latin")
SITE = "https://brendanbaehr-lab.github.io/latin-shows/latin/"
STAMP = "%Y-%m-%dT%H:%M:%SZ"

# Each city: centre and radius in miles. Wide enough for the arenas Latin tours play
# (Prudential Center and UBS Arena; Honda Center and Toyota Arena; Amerant Bank Arena in Sunrise).
CITIES = [("ny", "New York", 40.7306, -73.9866, 30), ("la", "Los Angeles", 34.0522, -118.2437, 45),
          ("mia", "Miami", 25.7617, -80.1918, 33)]
CITY_NAME = {k: n for k, n, *_ in CITIES}
NEW_DAYS = 7          # "new" on the page: first seen in the last week
AHEAD_DAYS = 400      # how far out to list
KEEP_SEEN_DAYS = 60   # a first-seen time outlives its show this long (a show briefly delisted isn't news again)
# Ticketmaster lists add-ons as their own events. (tm_rules.SKIP_NAMES is broader: its
# "experience", "sessions" and "in concert" would drop real shows here.)
NOT_A_SHOW = re.compile(r"(?i)\b(parking|vip|package|upgrade|suite|premium|club access|meet ?(&|and) ?greet|"
                        r"hospitality|lounge access|merch|shuttle|camping|locker|fast lane|soundcheck)\b")


# ---------------------------------------------------------------- the list (encrypted)

def fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("CLIENTS_KEY")
    if not key and os.path.exists(os.path.join(HERE, ".clients-key")):
        key = open(os.path.join(HERE, ".clients-key")).read()
    if not key:
        sys.exit("No client key: set CLIENTS_KEY (GitHub secret) or put it in .clients-key")
    return Fernet(key.strip())


def load_list():
    if not os.path.exists(LIST):
        return {"artists": []}
    return json.loads(fernet().decrypt(open(LIST, "rb").read()))


def save_list(lst):
    lst["artists"].sort(key=lambda a: (a["group"] != "roster", norm(a["name"])))
    open(LIST, "wb").write(fernet().encrypt(json.dumps(lst, ensure_ascii=False).encode()))


def norm(s):
    """Names compared loosely: accents, case, punctuation, "&" and "Ø" (RØZ) don't matter."""
    s = str(s or "").replace("Ø", "O").replace("ø", "o")
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = re.sub(r"\(.*?\)", " ", s.lower().replace("&", " and "))
    return re.sub(r"^the ", "", re.sub(r"[^a-z0-9]+", " ", s).strip())


def named(a, name):
    return norm(name) in (norm(a["name"]), norm(a.get("display")))


def find(lst, name):
    hit = [a for a in lst["artists"] if named(a, name)]
    if not hit:
        sys.exit(f'"{name}" is not on the list')
    return hit[0]


# ---------------------------------------------------------------- Ticketmaster

def client():
    import ticketmaster as tm
    key = tm.api_key()
    if not key:
        sys.exit("TICKETMASTER_API_KEY is not set (environment, .env, or the GitHub secret)")
    os.makedirs(tm.OUT, exist_ok=True)
    return tm.Client(key)


def resolve(c, name):
    """The Ticketmaster attraction for a name: an exact name match only (Bolo has five near
    misses); among exact matches, the one with the most upcoming events. None if no match."""
    r = c.get("attractions.json", keyword=name, size=20, locale="*")
    exact = [a for a in r.get("_embedded", {}).get("attractions", []) if norm(a["name"]) == norm(name)]
    if not exact:
        return None
    a = max(exact, key=lambda a: sum(a.get("upcomingEvents", {}).get(k, 0) for k in ("ticketmaster", "_total")))
    return {"id": a["id"], "name": a["name"]}


def miles(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    h = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def where(v):
    """City key for a Ticketmaster venue, or None when it is outside all three."""
    loc = v.get("location") or {}
    try:
        lat, lon = float(loc["latitude"]), float(loc["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    for key, _, clat, clon, radius in CITIES:
        if miles(lat, lon, clat, clon) <= radius:
            return key
    return None


def events(c, tmid):
    out, page = [], 0
    end = (datetime.date.today() + datetime.timedelta(days=AHEAD_DAYS)).strftime("%Y-%m-%dT23:59:59Z")
    while True:
        r = c.get("events.json", attractionId=tmid, size=200, page=page, sort="date,asc", endDateTime=end, locale="*")
        out += r.get("_embedded", {}).get("events", [])
        if page + 1 >= r.get("page", {}).get("totalPages", 0) or page >= 4:
            return out
        page += 1


BLANK_SHOW = {"time": None, "event": None, "tickets": None, "onsale": None, "presale": None,
              "price": None, "status": None, "country": "US"}


def show_from(e, artist):
    """One Discovery event as a row: ticketmaster.record() plus where the venue is."""
    import ticketmaster as tm
    r = tm.record(e)
    v = (e.get("_embedded", {}).get("venues") or [{}])[0]
    p = sorted(r["prices"], key=lambda p: p.get("type") != "standard")
    return {
        "id": "tm:" + r["tmId"], "artist": artist["display"], "group": artist["group"],
        "date": r["date"], "time": r["time"], "event": r["name"], "bill": r["acts"],
        "venue": r["venue"], "venueCity": ", ".join(x for x in (r["city"], r["state"]) if x),
        "city": where(v), "country": v.get("country", {}).get("countryCode"),
        "tickets": r["url"], "onsale": r["onsale"], "presale": r["presale"],
        "price": [p[0].get("min"), p[0].get("max"), p[0].get("currency")] if p else None,
        "status": r["status"] if r["status"] in ("cancelled", "postponed", "rescheduled") else None,
        "source": "Ticketmaster",
    }


def sheet_rows(artists):
    """THE·TEAM's client sheet (NYC and LA tabs only), when decrypted here: dates Ticketmaster
    doesn't sell. Agents are never copied out."""
    by = {}
    for a in artists:
        if a["group"] == "roster":
            by[norm(a["name"])] = by[norm(a["display"])] = a
    out = []
    for city, key in (("nyc", "ny"), ("la", "la")):
        p = os.path.join(HERE, "data", f"clients_raw_{city}.json")
        if not os.path.exists(p):
            continue
        for r in json.load(open(p)):
            a = by.get(norm(r[1]))
            if a and r[0] and r[0] >= str(datetime.date.today()):
                slug = lambda x: norm(x).replace(" ", "-")
                out.append({**BLANK_SHOW, "id": f"sheet:{r[0]}:{slug(r[1])}:{slug(r[2])}",
                            "artist": a["display"], "group": "roster", "date": r[0], "bill": [a["display"]],
                            "venue": str(r[2]), "venueCity": ", ".join(str(x) for x in (r[3], r[4]) if x),
                            "city": key, "source": "THE·TEAM client sheet"})
    return out


def lookup(c, a, today):
    """Find the artist's Ticketmaster id by name: on the first run, then weekly while it misses."""
    week_ago = str(datetime.date.today() - datetime.timedelta(days=7))
    if a.get("tm") is None and a.get("noMatch", "") <= week_ago:
        hit = resolve(c, a["name"])
        if hit:
            a["tm"], a["display"] = hit["id"], hit["name"]
            a.pop("noMatch", None)
        else:
            a["noMatch"] = today


def refresh():
    import ticketmaster as tm
    lst = load_list()
    if not lst["artists"]:
        sys.exit("The list is empty: run  python3 latin.py init roster.txt")
    today = str(datetime.date.today())
    c = client()
    shows, unmatched = [], []
    try:
        for a in lst["artists"]:
            a.setdefault("display", a["name"].title())
            lookup(c, a, today)
            if a.get("tm") in (None, "none"):
                unmatched.append(a["display"])
                continue
            a.setdefault("tracked", today)
            for e in events(c, a["tm"]):
                venue = ((e.get("_embedded", {}).get("venues") or [{}])[0].get("name") or "")
                if NOT_A_SHOW.search(e.get("name") or "") or "redirect" in venue.lower():
                    continue  # add-ons, and "Autódromo ... - Redirect" copies filed under the wrong city
                s = show_from(e, a)
                if s["date"]:
                    shows.append(s)
    except tm.OutOfQuota as err:
        sys.exit(f"Stopped: {err}. Nothing was rebuilt.")
    finally:
        c.save()
    save_list(lst)

    # Client-sheet dates Ticketmaster doesn't have (same artist, same date: already listed)
    have = {(norm(s["artist"]), s["date"]) for s in shows}
    shows += [s for s in sheet_rows(lst["artists"]) if (norm(s["artist"]), s["date"]) not in have]

    # One row per artist, night and city. Ticketmaster often lists a night twice: a resale or
    # "official platinum" copy, or the old room's placeholder when a show moves ("Paloma Morphy
    # (moved to Mercury Lounge)"). The plain listing, with the shortest name, is the real one.
    best = {}
    for s in sorted(shows, key=lambda s: (s["source"] != "Ticketmaster", bool(s["status"]), len(s["event"] or ""), s["id"])):
        best.setdefault((norm(s["artist"]), s["date"], s["city"] or s["venueCity"]), s)
    shows = sorted(best.values(), key=lambda s: (s["date"], s["time"] or "99", s["artist"]))

    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime(STAMP)
    seen = json.load(open(SEEN)) if os.path.exists(SEEN) else {}
    fresh = {a["display"] for a in lst["artists"] if a.get("tracked") == today}  # first lookup: a baseline
    for s in shows:
        seen.setdefault(s["id"], "baseline" if s["artist"] in fresh else stamp)
        f = seen[s["id"]]
        s["firstSeen"] = f
        s["new"] = f != "baseline" and now - datetime.datetime.strptime(f, STAMP).replace(tzinfo=datetime.timezone.utc) \
            <= datetime.timedelta(days=NEW_DAYS)
        s["note"] = onsale_note(s, stamp)
    ids, cutoff = {s["id"] for s in shows}, (now - datetime.timedelta(days=KEEP_SEEN_DAYS)).strftime(STAMP)
    seen = {k: v for k, v in seen.items() if k in ids or (v != "baseline" and v >= cutoff)}
    json.dump(dict(sorted(seen.items())), open(SEEN, "w"), indent=0)
    shows = [s for s in shows if s["date"] >= today]
    build(lst["artists"], shows, unmatched, now)


# ---------------------------------------------------------------- outputs

def day(d):
    x = datetime.date.fromisoformat(d)
    return x.strftime("%a %b ") + str(x.day) + ("" if x.year == datetime.date.today().year else x.strftime(", %Y"))


def clock(t):
    if not t:
        return ""
    h, m = map(int, t.split(":"))
    return f"{(h % 12) or 12}{'' if m == 0 else f':{m:02d}'}{'pm' if h >= 12 else 'am'}"


def onsale_note(s, stamp):
    """The next presale or on-sale still ahead, or ""."""
    ahead = [(t, label) for t, label in ((s["presale"], "Presale"), (s["onsale"], "On sale")) if t and t > stamp]
    if not ahead:
        return ""
    t, label = min(ahead)
    return f"{label} {day(t[:10])}"


def elsewhere_line(v):
    return f'{v[0]["date"]} {v[0]["venueCity"] or v[0]["country"] or ""}'


def build(artists, shows, unmatched, now):
    os.makedirs(OUT, exist_ok=True)
    stamp = now.strftime(STAMP)
    ours = [s for s in shows if s["city"]]
    elsewhere = {}
    for s in shows:
        if not s["city"]:
            elsewhere.setdefault(s["artist"], []).append(s)
    roster = [a["display"] for a in artists if a["group"] == "roster"]
    pursuits = [a["display"] for a in artists if a["group"] == "pursuit"]
    booked = {s["artist"] for s in ours}
    new = [s for s in ours if s["new"]]

    # feed.json: what a Custom GPT action reads. Compact; ChatGPT truncates very large responses.
    feed = {
        "title": "THE·TEAM Latin roster and pursuits: shows in New York, Los Angeles and Miami",
        "updated": stamp,
        "about": ("Roster artists are signed to THE·TEAM. Pursuits are artists THE·TEAM is pursuing and does NOT "
                  "represent; always label them as pursuits, never as clients. Dates come from the Ticketmaster "
                  "Discovery API (every date per artist) and, for NYC and LA, THE·TEAM's client sheet. Shows sold only "
                  "through AXS, DICE, Eventbrite or a venue's own site may be missing. 'new' means first seen in the "
                  f"last {NEW_DAYS} days; firstSeen 'baseline' means it was already on sale when tracking began."),
        "page": SITE,
        "cities": CITY_NAME,
        "roster": roster, "pursuits": pursuits,
        "noTicketmasterMatch": unmatched,
        "shows": [{k: v for k, v in {
            "artist": s["artist"], "group": s["group"], "city": CITY_NAME[s["city"]], "date": s["date"], "time": s["time"],
            "venue": s["venue"], "venueCity": s["venueCity"], "bill": s["bill"] if len(s["bill"]) > 1 else None,
            "event": s["event"] if s["event"] and norm(s["event"]) != norm(s["artist"]) else None,
            "tickets": s["tickets"], "presale": s["presale"], "onsale": s["onsale"],
            "price": s["price"], "status": s["status"], "new": s["new"], "firstSeen": s["firstSeen"],
            "source": s["source"]}.items() if v not in (None, [], False) or k == "new"} for s in ours],
        "elsewhere": {a: {"count": len(v), "next": [elsewhere_line([x]) for x in v[:3]]} for a, v in sorted(elsewhere.items())},
    }
    json.dump(feed, open(os.path.join(OUT, "feed.json"), "w"), ensure_ascii=False, indent=1)
    write_text(ours, new, roster, pursuits, booked, unmatched, feed)
    write_openapi()
    write_page(ours, new, elsewhere, artists, booked, roster, pursuits, unmatched, now)
    print(f"{len(ours)} shows in NY/LA/Miami ({len(new)} new), {sum(map(len, elsewhere.values()))} elsewhere; "
          f"{len(roster)} roster, {len(pursuits)} pursuits; no Ticketmaster match: {', '.join(unmatched) or 'none'}")


def write_text(ours, new, roster, pursuits, booked, unmatched, feed):
    """feed.txt: the same data, for a person or ChatGPT's browser."""
    def line(s, *parts):
        tags = [s["status"].upper()] if s["status"] else []
        tags += [s["note"]] if s["note"] else []
        tags += ["NEW"] if s["new"] else []
        return " | ".join([*parts, *tags, s["tickets"] or s["source"]])

    L = [feed["title"], f"Updated {feed['updated']} UTC. {feed['about']}", "", f"NEW IN THE LAST {NEW_DAYS} DAYS ({len(new)})"]
    for s in new:
        name = s["artist"] + (" [PURSUIT, not signed]" if s["group"] == "pursuit" else "")
        L.append("- " + line(s, name, day(s["date"]), f'{s["venue"]}, {CITY_NAME[s["city"]]}'))
    if not new:
        L.append("- none")
    for grp, head in (("roster", "ROSTER"), ("pursuit", "PURSUITS (not signed; tracked only)")):
        L += ["", head]
        for key, name in CITY_NAME.items():
            rows = [s for s in ours if s["group"] == grp and s["city"] == key]
            L.append(f"  {name.upper()} ({len(rows)})")
            L += [f"  - " + line(s, f'{day(s["date"])} {clock(s["time"])}'.strip(), s["artist"], f'{s["venue"]} ({s["venueCity"]})')
                  for s in rows] or ["  - none"]
    L += ["", "ROSTER WITH NOTHING IN NY, LA OR MIAMI: " + (", ".join(a for a in roster if a not in booked) or "none"),
          "PURSUITS WITH NOTHING IN NY, LA OR MIAMI: " + (", ".join(a for a in pursuits if a not in booked) or "none")]
    if unmatched:
        L.append("NOT FOUND ON TICKETMASTER (pin the right id): " + ", ".join(unmatched))
    open(os.path.join(OUT, "feed.txt"), "w").write("\n".join(L) + "\n")


def write_openapi():
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "THE·TEAM Latin shows", "version": "1.0",
                 "description": "Upcoming shows in New York, Los Angeles and Miami for THE·TEAM's Latin roster and pursuits."},
        "servers": [{"url": SITE.rstrip("/")}],
        "paths": {"/feed.json": {"get": {
            "operationId": "getLatinShows",
            "summary": "All upcoming NY/LA/Miami shows for the Latin roster and pursuits, refreshed daily",
            "responses": {"200": {"description": "The feed", "content": {"application/json": {"schema": {"type": "object"}}}}}}}},
    }
    json.dump(spec, open(os.path.join(OUT, "openapi.json"), "w"), indent=1, ensure_ascii=False)


PURSUIT_TAG = '<span class="tag pursuit">Pursuit</span>'


def write_page(ours, new, elsewhere, artists, booked, roster, pursuits, unmatched, now):
    e = html.escape
    upd = now.astimezone(ZoneInfo("America/New_York"))
    updated = upd.strftime("%b ") + str(upd.day) + upd.strftime(", %Y, ") + clock(upd.strftime("%H:%M")) + " ET"
    groups = {a["display"]: a["group"] for a in artists}

    def row(s, with_city=False):
        tags = PURSUIT_TAG if s["group"] == "pursuit" else ""
        tags += '<span class="tag new">New</span>' if s["new"] else ""
        tags += f'<span class="tag warn">{e(s["status"].title())}</span>' if s["status"] else ""
        name = e(s["artist"])
        if s["tickets"]:
            name = f'<a href="{e(s["tickets"])}" target="_blank" rel="noopener">{name}</a>'
        others = [b for b in s["bill"] if norm(b) != norm(s["artist"])]
        sub = f"with {', '.join(others)}" if others else s["event"] if s["event"] and norm(s["event"]) != norm(s["artist"]) else ""
        place = CITY_NAME[s["city"]] if with_city else s["venueCity"]
        return (f'<li class="show"><div class="when"><b>{e(day(s["date"]))}</b><span>{clock(s["time"])}</span></div>'
                f'<div class="what"><div class="act">{name}{tags}</div>'
                + (f'<span class="with">{e(sub)}</span>' if sub else "")
                + f'<div class="venue">{e(s["venue"] or "")}' + (f' <span class="muted">· {e(place)}</span>' if place else "")
                + f'</div></div><div class="sale">{e(s["note"] if s["tickets"] else s["source"])}</div></li>')

    def rows(lst, empty, with_city=False):
        return f'<ul class="list">{"".join(row(s, with_city) for s in lst)}</ul>' if lst else f'<p class="empty">{empty}</p>'

    parts = [f'<section class="block"><h2>New this week <small>{len(new)}</small></h2>',
             rows(new, f"Nothing newly announced in the last {NEW_DAYS} days.", True), '</section>']
    for key, name in CITY_NAME.items():
        r = [s for s in ours if s["city"] == key and s["group"] == "roster"]
        parts += [f'<section class="block city" id="{key}"><h2>{e(name)} <small>{len(r)}</small></h2>',
                  rows(r, "No roster dates."), '</section>']
    prow = [s for s in ours if s["group"] == "pursuit"]
    parts.append(f'<section class="block pursuits" id="pursuits"><h2>Pursuits <small>{len(prow)}</small></h2>'
                 '<p class="note">Artists THE·TEAM is pursuing. Not signed; tracked so the team can see them play.</p>')
    if pursuits:
        for key, name in CITY_NAME.items():
            r = [s for s in prow if s["city"] == key]
            if r:
                parts.append(f'<h3>{e(name)}</h3>' + rows(r, ""))
        idle = [a for a in pursuits if a not in booked]
        if idle:
            parts.append(f'<p class="muted small">No NY, LA or Miami dates: {e(", ".join(idle))}</p>')
    else:
        parts.append('<p class="empty">No pursuits yet. Add one from the GitHub Actions tab: "Latin roster" &gt; Run workflow.</p>')
    parts.append('</section>')

    els = "".join(f'<li><b>{e(a)}</b> {len(v)} date{"s" * (len(v) != 1)}, next {e(day(v[0]["date"]))}, '
                  f'{e(v[0]["venueCity"] or v[0]["country"] or "")}{PURSUIT_TAG if groups.get(a) == "pursuit" else ""}</li>'
                  for a, v in sorted(elsewhere.items(), key=lambda x: x[1][0]["date"]))
    idle = ", ".join(a for a in roster if a not in booked) or "none"
    missing = f'<p class="small">Not found on Ticketmaster (the list needs their id): {e(", ".join(unmatched))}</p>' if unmatched else ""
    parts.append(f'<section class="block"><h2>Elsewhere</h2><p class="muted small">Roster with no NY, LA or Miami dates: {e(idle)}.</p>'
                 f'{missing}<details><summary>Touring outside the three cities ({len(elsewhere)} artists)</summary>'
                 f'<ul class="els">{els}</ul></details></section>')
    parts.append('<section class="block"><h2>Email report</h2><p class="small">ChatGPT reads this data itself: '
                 '<a href="feed.json">feed.json</a> (for a Custom GPT action, schema <a href="openapi.json">openapi.json</a>) '
                 'or <a href="feed.txt">feed.txt</a> (plain text). Setup: <a href="gpt.html">gpt.html</a>.</p></section>')

    nav = "".join(f'<a href="#{k}">{e(n)}</a>' for k, n in CITY_NAME.items()) + '<a href="#pursuits">Pursuits</a>'
    page = (PAGE.replace("{{UPDATED}}", e(updated)).replace("{{NAV}}", nav).replace("{{BODY}}", "\n".join(parts))
            .replace("{{N}}", str(len(ours))).replace("{{R}}", str(len(roster))).replace("{{P}}", str(len(pursuits))))
    open(os.path.join(OUT, "index.html"), "w").write(page)


# Tokens from page.html's :root (THE·TEAM identity); keep the two in step.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Latin Roster Shows</title>
<style>
:root { --bg:#fffdf8; --ink:#100906; --grey7:#97999b; --grey1:#d9d9d6; --muted:#6f7173;
  --accent:#2e7040; --tint:#e2ece4; --amber:#8a5a00;
  --sans: Arial, "Helvetica Neue", Helvetica, sans-serif; --serif: "Times New Roman", Times, serif; color-scheme: light; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 var(--sans); letter-spacing:-.02em; -webkit-font-smoothing:antialiased; }
a { color: inherit; }
.wrap { max-width: 920px; margin: 0 auto; padding-inline: clamp(16px, 4vw, 40px); }
.label { font-size:10px; font-weight:500; letter-spacing:.1em; text-transform:uppercase; }
.mast { border-bottom:1px solid var(--grey1); }
.mast .wrap { display:flex; justify-content:space-between; gap:12px; padding-block:18px; flex-wrap:wrap; color:var(--muted); }
.mast a { text-decoration:none; color:var(--ink); }
.dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--accent); margin-right:8px; }
.hero { padding-block: 32px 8px; }
h1 { font:400 clamp(30px,4.2vw,44px)/1 var(--serif); margin:0; letter-spacing:-.02em; }
h1 em { color:var(--accent); }
.hero p { color:var(--muted); margin:14px 0 0; max-width:60ch; }
.jump { display:flex; gap:6px 22px; flex-wrap:wrap; padding-block:18px; border-bottom:1px solid var(--grey1); }
.jump a { font:20px var(--serif); text-decoration:none; color:var(--grey7); }
.jump a:hover { color:var(--ink); }
.block { padding-block: 28px 8px; border-bottom:1px solid var(--grey1); }
h2 { font:400 26px var(--serif); margin:0 0 12px; }
h2 small { font:500 11px var(--sans); letter-spacing:.1em; color:var(--grey7); margin-left:6px; vertical-align:4px; }
h3 { font:500 11px var(--sans); letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin:18px 0 4px; }
.list { list-style:none; margin:0; padding:0; }
.show { display:grid; grid-template-columns: 120px 1fr auto; gap:4px 16px; padding:12px 0; border-top:1px solid var(--grey1); }
.show:first-child { border-top:0; }
.when b { display:block; font-weight:500; }
.when span, .muted { color:var(--muted); }
.act { font-size:16px; font-weight:500; display:flex; flex-wrap:wrap; align-items:center; gap:6px; }
.act a { text-decoration:none; } .act a:hover { text-decoration:underline; }
.with { display:block; color:var(--muted); }
.venue { margin-top:2px; }
.sale { color:var(--accent); font-size:12px; text-align:right; white-space:nowrap; }
.tag { font:500 10px var(--sans); letter-spacing:.08em; text-transform:uppercase; padding:2px 6px; border:1px solid; }
.tag.new { color:var(--accent); background:var(--tint); border-color:var(--tint); }
.tag.pursuit { color:var(--amber); border-color:var(--amber); border-style:dashed; }
.tag.warn { color:#a3221a; border-color:#a3221a; }
.pursuits { background: repeating-linear-gradient(-45deg, transparent 0 10px, rgba(138,90,0,.035) 10px 11px); }
.pursuits .note { color:var(--amber); margin:-4px 0 8px; }
.empty { color:var(--muted); }
.small { font-size:13px; }
details { margin:6px 0 16px; } summary { cursor:pointer; color:var(--muted); }
.els { padding-left:18px; color:var(--muted); } .els b { color:var(--ink); font-weight:500; }
footer { padding-block:24px 48px; color:var(--muted); font-size:12px; }
@media (max-width:600px) { .show { grid-template-columns: 1fr; } .sale { text-align:left; } }
</style>
</head>
<body>
<header class="mast"><div class="wrap label"><span><span class="dot"></span>Updated {{UPDATED}}</span><a href="../">All Latin shows &rarr;</a></div></header>
<main class="wrap">
<div class="hero"><h1>Latin roster, <em>NY · LA · Miami</em></h1>
<p>{{N}} upcoming shows for {{R}} roster artists and {{P}} pursuits, from Ticketmaster and THE·TEAM's client sheet. Refreshed every morning. Dates sold only through AXS, DICE or a venue's own site may be missing.</p></div>
<nav class="jump" aria-label="Sections">{{NAV}}</nav>
{{BODY}}
</main>
<footer class="wrap">Pursuits are not THE·TEAM clients. Ticket links go to Ticketmaster.</footer>
</body>
</html>
"""


# ---------------------------------------------------------------- commands

def main(argv):
    cmd = argv[0] if argv else "refresh"
    if cmd == "refresh":
        return refresh()
    lst = load_list()
    if cmd == "init":
        names = [l.strip() for l in open(argv[1]) if l.strip()]
        lst["artists"] += [{"name": n, "group": "roster", "tm": None, "added": str(datetime.date.today())}
                           for n in names if not any(named(a, n) for a in lst["artists"])]
    elif cmd == "list":
        for a in lst["artists"]:
            print(f'{a["group"]:8} {a.get("display", a["name"]):32} {a.get("tm") or "-"}')
        return
    elif cmd == "add":
        name = argv[1].strip()
        if any(named(a, name) for a in lst["artists"]):
            sys.exit(f'"{name}" is already on the list')
        tmid = argv[argv.index("--tm") + 1] if "--tm" in argv else None
        lst["artists"].append({"name": name, "group": "roster" if "--roster" in argv else "pursuit",
                               "tm": tmid, "added": str(datetime.date.today())})
    elif cmd == "sign":
        find(lst, argv[1])["group"] = "roster"
    elif cmd == "remove":
        lst["artists"].remove(find(lst, argv[1]))
    elif cmd == "pin":
        # an id; "none" = not on Ticketmaster, stop looking; "auto" = search by name again
        a = find(lst, argv[1])
        a["tm"] = None if argv[2] == "auto" else argv[2]
        a.pop("noMatch", None)
    else:
        sys.exit(__doc__)
    save_list(lst)
    print(f'{cmd}: done. {sum(a["group"] == "roster" for a in lst["artists"])} roster, '
          f'{sum(a["group"] == "pursuit" for a in lst["artists"])} pursuits')


if __name__ == "__main__":
    main(sys.argv[1:])
