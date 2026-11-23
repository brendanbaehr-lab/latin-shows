"""Every Latin show in New York, Los Angeles and Miami, from the Ticketmaster Discovery API.

    python3 shows.py        sweep Ticketmaster, rebuild index.html

One sweep per city pulls every event Ticketmaster files under the Latin genre within the
city's radius for the next year, 200 at a time. data/shows_seen.json records when each event
was first seen (ids and times only); that is how the page knows what was just announced.
The first sweep is a baseline: nothing on it counts as just announced.

Outputs: index.html (the page, data inside it) and shows.json (the same rows, for anything else).
The key comes from TICKETMASTER_API_KEY (environment, .env, or the GitHub secret).
"""
import datetime, html, json, os, sys
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


def sweep(c, key, lat, lon, radius):
    out = {}

    def window(a, b):
        params = dict(genreId=LATIN, geoPoint=tm.geohash(lat, lon), radius=radius, unit="miles", size=200,
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
                if where(v) != key:
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


def write_page(rows, now):
    upd = now.astimezone(ZoneInfo("America/New_York"))
    updated = upd.strftime("%b ") + str(upd.day) + upd.strftime(", %Y, ") + clock(upd.strftime("%H:%M")) + " ET"
    cutoff = (now - datetime.timedelta(days=NEW_DAYS)).strftime(STAMP)
    soon = str(datetime.date.today() + datetime.timedelta(days=SOON_DAYS))
    data = [{"d": s["date"], "day": day(s["date"]), "t": clock(s["time"]), "h": s["headliner"], "w": s["support"],
             "v": s["venue"] or "", "vc": s["venueCity"], "c": s["city"], "g": s["subgenre"] or "",
             "u": s["tickets"], "n": s["note"], "x": (s["status"] or "").title(),
             "soon": s["date"] <= soon, "new": s["firstSeen"] != "baseline" and s["firstSeen"] >= cutoff,
             "f": s["firstSeen"]} for s in rows]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (PAGE.replace("{{UPDATED}}", html.escape(updated)).replace("{{DATA}}", blob)
            .replace("{{N}}", str(len(rows))).replace("{{NEWDAYS}}", str(NEW_DAYS)))
    open(os.path.join(HERE, "index.html"), "w").write(page)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Latin Shows: NY, LA, Miami</title>
<meta name="description" content="Every Latin show in New York, Los Angeles and Miami, from Ticketmaster. Updated every morning.">
<style>
:root { --bg:#fffdf8; --ink:#100906; --grey7:#97999b; --grey1:#d9d9d6; --muted:#6f7173;
  --accent:#2e7040; --tint:#e2ece4; --warn:#a3221a;
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
.hero p { color:var(--muted); margin:14px 0 0; max-width:62ch; }
.controls { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; padding-block:20px 16px; border-bottom:1px solid var(--grey1); }
.seg { display:inline-flex; border:1px solid var(--ink); }
.seg button { font:500 12px var(--sans); letter-spacing:.02em; background:transparent; color:var(--ink); border:0; padding:8px 12px; cursor:pointer; }
.seg button + button { border-left:1px solid var(--ink); }
.seg button[aria-pressed="true"] { background:var(--ink); color:var(--bg); }
.seg button:hover:not([aria-pressed="true"]) { background:var(--tint); }
.toggle { display:inline-flex; gap:6px; align-items:center; font-size:13px; cursor:pointer; }
.toggle input { accent-color: var(--accent); width:15px; height:15px; }
.search { flex:1 1 180px; min-width:0; font:14px var(--sans); padding:8px 10px; border:1px solid var(--grey1); background:#fff; color:var(--ink); }
.count { color:var(--muted); font-size:13px; padding-block:14px 0; }
h2 { font:400 22px var(--serif); margin:22px 0 4px; }
.list { list-style:none; margin:0; padding:0; }
.show { display:grid; grid-template-columns: 120px 1fr auto; gap:4px 16px; padding:12px 0; border-top:1px solid var(--grey1); }
.when b { display:block; font-weight:500; }
.when span, .muted { color:var(--muted); }
.act { font-size:16px; font-weight:500; display:flex; flex-wrap:wrap; align-items:center; gap:6px; }
.act a { text-decoration:none; } .act a:hover { text-decoration:underline; }
.with { display:block; color:var(--muted); }
.venue { margin-top:2px; }
.sale { color:var(--accent); font-size:12px; text-align:right; white-space:nowrap; }
.tag { font:500 10px var(--sans); letter-spacing:.08em; text-transform:uppercase; padding:2px 6px; border:1px solid; }
.tag.new { color:var(--accent); background:var(--tint); border-color:var(--tint); }
.tag.solo { color:var(--muted); border-color:var(--grey1); }
.tag.warn { color:var(--warn); border-color:var(--warn); }
.empty { color:var(--muted); padding-block:16px; }
footer { padding-block:28px 48px; color:var(--muted); font-size:12px; }
@media (max-width:600px) { .show { grid-template-columns: 1fr; } .sale { text-align:left; } }
</style>
</head>
<body>
<header class="mast"><div class="wrap label"><span><span class="dot"></span>Updated {{UPDATED}}</span><a href="latin/">Latin roster &rarr;</a></div></header>
<main class="wrap">
<div class="hero"><h1>Latin shows, <em>NY · LA · Miami</em></h1>
<p>Every show Ticketmaster files as Latin in the three cities, {{N}} in all over the next year. Refreshed every morning. Shows sold only through AXS, DICE or a venue's own site are missing.</p></div>
<div class="controls">
  <div class="seg" role="group" aria-label="View">
    <button type="button" data-view="soon" aria-pressed="true">Next two weeks</button><button type="button" data-view="new" aria-pressed="false">Just announced</button><button type="button" data-view="all" aria-pressed="false">All dates</button>
  </div>
  <div class="seg" role="group" aria-label="City">
    <button type="button" data-city="" aria-pressed="true">All</button><button type="button" data-city="ny" aria-pressed="false">New York</button><button type="button" data-city="la" aria-pressed="false">Los Angeles</button><button type="button" data-city="mia" aria-pressed="false">Miami</button>
  </div>
  <label class="toggle"><input type="checkbox" id="solo"> No support only</label>
  <input class="search" id="q" type="search" placeholder="Search artist or venue" aria-label="Search artist or venue">
</div>
<p class="count" id="count" aria-live="polite"></p>
<div id="out"></div>
</main>
<footer class="wrap">Source: Ticketmaster Discovery API, genre Latin. Just announced means first seen here in the last {{NEWDAYS}} days. "No support" means Ticketmaster lists one act on the bill.</footer>
<script>
const SHOWS = {{DATA}};
const CITY = {ny: "New York", la: "Los Angeles", mia: "Miami"};
const st = {view: "soon", city: "", solo: false, q: ""};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fold = s => String(s ?? "").normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
function row(s) {
  const name = s.u ? `<a href="${esc(s.u)}" target="_blank" rel="noopener">${esc(s.h)}</a>` : esc(s.h);
  const tags = (s.new ? '<span class="tag new">New</span>' : "") + (!s.w.length ? '<span class="tag solo">No support</span>' : "")
    + (s.x ? `<span class="tag warn">${esc(s.x)}</span>` : "");
  const place = st.city ? s.vc : CITY[s.c];
  return `<li class="show"><div class="when"><b>${esc(s.day)}</b><span>${esc(s.t)}</span></div>
    <div class="what"><div class="act">${name}${tags}</div>${s.w.length ? `<span class="with">with ${esc(s.w.join(", "))}</span>` : ""}
    <div class="venue">${esc(s.v)}${place ? ` <span class="muted">· ${esc(place)}</span>` : ""}${s.g && s.g !== "Latin" ? ` <span class="muted">· ${esc(s.g)}</span>` : ""}</div></div>
    <div class="sale">${esc(s.n)}</div></li>`;
}
function render() {
  const q = fold(st.q);
  let list = SHOWS.filter(s => (st.view === "soon" ? s.soon : st.view === "new" ? s.new : true)
    && (!st.city || s.c === st.city) && (!st.solo || !s.w.length)
    && (!q || fold([s.h, ...s.w, s.v].join(" ")).includes(q)));
  if (st.view === "new") list = list.slice().sort((a, b) => b.f.localeCompare(a.f) || a.d.localeCompare(b.d));
  document.getElementById("count").textContent = `${list.length} show${list.length === 1 ? "" : "s"}`;
  const out = document.getElementById("out");
  if (!list.length) { out.innerHTML = `<p class="empty">${st.view === "new" ? "Nothing newly announced yet. New listings show up here after the next daily sweep." : "No shows match."}</p>`; return; }
  if (st.view === "new") { out.innerHTML = `<ul class="list">${list.map(row).join("")}</ul>`; return; }
  const byMonth = {};
  list.forEach(s => { const m = new Date(s.d + "T12:00:00").toLocaleString("en-US", {month: "long", year: "numeric"}); (byMonth[m] ||= []).push(s); });
  out.innerHTML = Object.entries(byMonth).map(([m, xs]) => `<h2>${m}</h2><ul class="list">${xs.map(row).join("")}</ul>`).join("");
}
document.querySelectorAll(".seg").forEach(g => g.addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  g.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === b));
  if ("view" in b.dataset) st.view = b.dataset.view; else st.city = b.dataset.city;
  render();
}));
document.getElementById("solo").addEventListener("change", e => { st.solo = e.target.checked; render(); });
document.getElementById("q").addEventListener("input", e => { st.q = e.target.value; render(); });
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
