# Web crawler pathfinder

A web app: drop any URL in the center box and it dispatches concurrent crawlers
that breadth-first their way across the open web until they reach
**github.com**. `google.com` counts as a goal too — once you're on Google you
can just search your way to GitHub. The first route found is the **shortest by
number of steps**, and you watch the search happen live as a tree.

The match is **exact**: only the hosts `github.com` and `google.com` (with or
without a leading `www.`) end the search. Subdomains such as
`policies.google.com` or `gist.github.com` do *not* count.

### Live search tree

As crawlers fan out, the page draws the **BFS search tree** in real time:
- grey = a link that was discovered, blue = a page that was actually crawled
  (expanded), and the winning **shortest path is highlighted in green**;
- nodes are collapsible, and the tree view is capped at 800 nodes for
  readability (the underlying search keeps going).

### The path, spelled out

Once a route is found, a section at the bottom lists **just the path** — one
row per step, each showing the **text of the link you'd click** on that page
and the URL it leads to (e.g. click *"Contribute on GitHub"* →
`https://github.com/pypi/warehouse`). The final step is flagged as the goal.

### Advanced options

Expand **Advanced options** under the search box to set:

- **Custom ending** — the goal host(s) to search for instead of
  github.com/google.com. Comma-separated, e.g. `wikipedia.org, reddit.com`.
  Scheme/`www.`/path/port are all stripped for you.
- **Match subdomains** — when on, any subdomain of a goal host counts
  (so `gist.github.com` would end the search); off by default (exact match).
- **Max pages** — safety cap on total pages discovered.
- **Links per page** — only follow the first N links on each page.
- **Request timeout** — per-page fetch timeout in seconds.
- **Number of paths** — how many routes to find. Each route leaves the start
  through a *different* link; a first link is only reused (the path then
  diverging at the next link) once all of the start's links are used up. Path 1
  is always the global shortest, and each path gets its own section.
- **Find all shortest paths** — when on, ignores "Number of paths" and instead
  returns **every** distinct path that achieves the minimum step count. It builds
  a full shortest-path DAG (all predecessors at each node's BFS distance) and
  enumerates every minimal route (bounded for safety).

Threads and Max steps stay on the main search box.

### JavaScript verification (optional)

The static crawler can't see links a page injects with JavaScript. So after the
normal (static) paths are found, an optional verification phase re-renders
**every page in the found paths** in a real headless browser (Playwright) and
looks for goal links that only exist after JS runs — e.g. GitHub's React
app-header button, or icon links on SPAs.

Any such JS-only link gives an alternative — often **shorter** — route, which
is reported as its own **"Javascript Path N"** section (in violet), *in addition
to* the requested number of static paths. The count is capped to roughly the
number of static paths found. Toggle it under **Advanced options** (on by
default); it self-disables gracefully if Playwright isn't installed.

To enable it:

```bash
pip install playwright
playwright install chromium
```

### Known paths (backtrack)

The crawler searches *forward* from your start page. **Known paths** let you seed
the search from the *other* end with links you already know about — useful when
a link is JS-only, behind auth, or otherwise invisible to the static crawler.

In the **Known paths** panel, add (and edit) entries of the form *page → the
page it links to* on the way to the goal:

- **Level 1:** a page that links to the **target** (e.g. `some-blog.com → github.com`).
- **Level 2+:** a page that links to one you already added (`other-site.com → some-blog.com`).

Each entry also has a **type** — *Click* (follow a link) or *Search* (search for
it) — which is shown on that step in the path output (e.g. "Search for …").
Entries can be edited inline.

This builds a user-curated reverse graph. If the forward crawl reaches **any**
of these pages, the route is completed through your trusted chain. These routes
compete with normal ones in the same shortest-path / diversity selection.

### Cutting links

Hover any node and click **✂** to *cut* that link: it's added to a block list
and the search instantly re-runs without it, routing around the cut. This is
useful when a link exists in the HTML but isn't actually clickable by a human —
e.g. it's visually hidden — so the "path" it created isn't a real one. Cut
links appear as chips you can remove individually or clear all at once.

As a bonus, the crawler also auto-skips anchors that are clearly hidden in
markup (`hidden`/`aria-hidden` attributes, or inline `display:none` /
`visibility:hidden` / `opacity:0`).

## How it works

The search is a **level-synchronous parallel BFS** (`crawler.py`):

1. Start from your URL (the only node at distance 0).
2. Fetch **every page in the current level concurrently** using a thread pool.
3. Collect all outbound links — they form the next level (distance + 1).
4. The first time a link points at a goal host, stop.

Because BFS expands strictly outward by distance, the first goal reached is
guaranteed to be at the end of a shortest path (fewest steps). We keep a `parent` map so
the path can be reconstructed by walking back to the start.

The Flask layer (`app.py`) runs the crawl in a background thread that pushes
progress events onto a queue, and streams them to the browser over
**Server-Sent Events** so the depth, page count, frontier size, and live log
all update in real time.

## Run it

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5050> and launch a crawl. (Port 5000 is taken by
AirPlay Receiver on macOS, so the app defaults to **5050**; override with
`PORT=8000 python app.py`.)

## Knobs

On the main search box: **Threads** (concurrent fetches per BFS level) and
**Max steps** (search depth). Everything else — custom ending, match-subdomains,
max pages, links per page, request timeout — lives under **Advanced options**
(see above). The same fields exist in `crawler.py` → `CrawlConfig`.

## Deploying to Render

The repo includes a `render.yaml` (native Python) and a `Dockerfile` (full,
with a real Chromium for JS verification). Push the project to a GitHub repo
first, then:

**Simple — native Python (free tier, no JS verification):**
1. On Render: **New → Blueprint**, point it at your repo. It reads `render.yaml`.
2. It builds with `pip install -r requirements.txt` and starts with
   `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0`.
3. Leave **Verify with JavaScript** off — the native runtime can't launch a
   browser, so it self-disables (you'll otherwise see a one-line notice).

**Full — Docker (JS verification works):**
1. On Render: **New → Web Service → your repo → Runtime: Docker**. It uses the
   `Dockerfile` (built on the official Playwright image with Chromium baked in).
2. Everything works, including JS verification. Needs a paid instance with
   enough memory — Chromium can OOM the 512 MB free tier.

Notes that matter for either path:
- The app already binds `$PORT`; gunicorn uses threaded workers and
  `--timeout 0` because the Server-Sent Events streams are long-lived.
- Keep it to a **single instance / 1 worker** — crawl state lives in memory per
  request, so multiple instances aren't needed (and wouldn't share state).
- Free instances spin down when idle, so the first request after a pause is slow.

## Notes & limits

- Most real pages link to GitHub or Google within 1–2 steps, so searches are
  usually fast. Link-poor pages can hit the depth limit.
- The main BFS fetches static HTML only (no JavaScript engine), so
  **client-rendered SPAs** whose links are injected by JS (e.g. `blank.page`)
  look linkless and dead-end during the search — a deliberate trade-off for
  speed. The optional [JavaScript verification](#javascript-verification-optional)
  phase only renders the pages that ended up *in* a found path, to catch
  JS-only shortcuts; it does not drive the whole crawl.
- Non-HTML assets, `mailto:`/`javascript:` links, and `#fragments` are filtered
  out; URLs are normalized for de-duplication.
- This crawler does **not** read `robots.txt` and is meant for light, ad-hoc
  use. Crawl responsibly and don't point it at infrastructure you don't own.

## Files

| File | Purpose |
| --- | --- |
| `crawler.py` | Parallel BFS engine + link extraction |
| `app.py` | Flask server + SSE progress stream |
| `templates/index.html` | Centered UI, live log, path visualization |
| `requirements.txt` | Flask, requests, beautifulsoup4 |
