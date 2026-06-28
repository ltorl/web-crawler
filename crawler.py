"""
Parallel breadth-first crawler that searches for the shortest hop-path
from a starting URL to github.com (google.com also counts as a goal,
since you can just google your way to GitHub from there).

The search is a classic level-synchronous BFS: every URL at the current
"distance" is fetched concurrently in a thread pool, all newly discovered
links become the next level, and the first time we discover a goal link we
stop. Because BFS expands strictly by distance, the first goal found is
guaranteed to sit at the end of a *shortest* path (fewest hops).

Progress is reported through a callback so the web layer can stream it live:
every discovered node is emitted with its parent so the UI can draw the
search tree, and the winning path is highlighted at the end.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

# Default hosts that count as "we made it". The match is EXACT (after stripping
# a leading "www." and any port) — github.com and google.com only. Subdomains
# like policies.google.com or gist.github.com do NOT count. The goal can be
# overridden per-crawl via CrawlConfig.goal_hosts (the "custom ending").
GOAL_HOSTS = ("github.com", "google.com")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; GithubPathCrawler/1.0; "
        "+https://example.local/bot) BFS path finder"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def host_of(url: str) -> str:
    """Lower-cased hostname without a leading 'www.' or port."""
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0]


def is_goal(url: str, goal_hosts=GOAL_HOSTS, match_subdomains: bool = False,
            allow_subpages: bool = True) -> bool:
    """True if the URL's host matches one of the goal hosts.

    By default the match is exact (after stripping a leading 'www.'), so
    policies.google.com does not count. With match_subdomains=True, any
    subdomain of a goal host counts too. With allow_subpages=False, only the
    site root counts (github.com/ yes, github.com/foo/bar no).
    """
    h = host_of(url)
    if match_subdomains:
        host_ok = any(h == g or h.endswith("." + g) for g in goal_hosts)
    else:
        host_ok = h in goal_hosts
    if not host_ok:
        return False
    if not allow_subpages and urlparse(url).path not in ("", "/"):
        return False
    return True


def normalize(url: str) -> str | None:
    """Canonicalize a URL for dedup; return None if it isn't worth crawling."""
    if not url:
        return None
    url, _frag = urldefrag(url)  # drop #fragment
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.netloc:
        return None
    lowered = parts.path.lower()
    bad_ext = (
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
        ".css", ".js", ".json", ".xml", ".pdf", ".zip", ".gz", ".mp4",
        ".mp3", ".woff", ".woff2", ".ttf", ".eot",
    )
    if lowered.endswith(bad_ext):
        return None
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc.lower()}{path}" + (
        f"?{parts.query}" if parts.query else ""
    )


def _anchor_hidden(a) -> bool:
    """Best-effort: is this <a> hidden from a normal user?

    Catches the common ways a link is present in the markup but not clickable
    by a human (the exact situation the manual "cut" feature also handles).
    """
    if a.has_attr("hidden"):
        return True
    if a.get("aria-hidden", "").lower() == "true":
        return True
    style = (a.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style or "opacity:0" in style:
        return True
    return False


def _octicon_label(cls: str) -> str:
    """Turn an octicon class (e.g. 'octicon-mark-github') into a label."""
    name = cls[len("octicon-"):]
    if name in ("mark-github", "logo-github"):
        return "GitHub"
    return name.replace("-", " ").strip()


def _anchor_text(a, soup) -> str:
    """The human-visible label of a link.

    Icon-only links (an <a> wrapping just an <svg>) have no text, so we fall
    back through the accessible-name sources a browser/screen-reader would use:
    aria-label, aria-labelledby (resolved against the document), title, a
    nested <img alt>, an <svg> title/aria-label, and finally the octicon class
    name (so a GitHub mark becomes "GitHub").
    """
    text = a.get_text(strip=True)
    if not text:
        text = a.get("aria-label", "")
    if not text and a.get("aria-labelledby"):
        # aria-labelledby is a space-separated list of element ids.
        parts = []
        for ref in a["aria-labelledby"].split():
            el = soup.find(id=ref)
            if el:
                parts.append(el.get_text(strip=True))
        text = " ".join(p for p in parts if p)
    if not text:
        text = a.get("title", "")
    if not text:
        img = a.find("img")
        if img:
            text = img.get("alt") or img.get("title") or ""
    if not text:
        svg = a.find("svg")
        if svg:
            title = svg.find("title")
            text = (svg.get("aria-label")
                    or (title.get_text(strip=True) if title else "")
                    or next((_octicon_label(c) for c in (svg.get("class") or [])
                             if c.startswith("octicon-")), ""))
    return " ".join((text or "").split())[:140]


# A discovered link is (normalized_url, anchor_text).
Link = tuple[str, str]


def extract_links(base_url: str, html: str) -> list[Link]:
    """Return normalized, deduped, crawlable, *visible* (url, text) links."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Link] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if _anchor_hidden(a):
            continue
        absolute = urljoin(base_url, a["href"].strip())
        norm = normalize(absolute)
        if norm and norm not in seen:
            seen.add(norm)
            out.append((norm, _anchor_text(a, soup)))
    return out


def fetch_links(url: str, timeout: float) -> list[Link]:
    """Download a page and return its outbound (url, text) links."""
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=timeout, allow_redirects=True
        )
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            return []
        links = extract_links(resp.url, resp.text)
        if resp.url != url:
            final = normalize(resp.url)
            if final and final not in {u for u, _ in links}:
                links.insert(0, (final, "(redirected here)"))
        return links
    except requests.RequestException:
        return []
    except Exception:
        return []


@dataclass
class CrawlConfig:
    workers: int = 16          # concurrent fetches per BFS level
    max_depth: int = 6         # give up after this many hops
    max_nodes: int = 4000      # safety cap on total pages discovered
    per_page_links: int = 40   # only enqueue the first N links of a page
    timeout: float = 8.0       # per-request timeout (seconds)
    goal_hosts: tuple = GOAL_HOSTS   # the "custom ending" hosts to search for
    match_subdomains: bool = False   # if True, subdomains of a goal count too
    allow_subpages: bool = True      # if False, only the goal site root counts
    num_paths: int = 1         # how many root-diverse paths to the goal to find
    all_shortest: bool = False # find EVERY path with the minimum hop count
    clicks_only: bool = False  # if True, known paths may only use "click" steps
    verify_js: bool = True     # after static paths, re-check pages with a real browser
    js_max_pages: int = 30     # cap on how many pages to JS-render during verify


@dataclass
class CrawlResult:
    found: bool
    path: list[str] = field(default_factory=list)
    visited: int = 0
    depth: int = 0
    elapsed: float = 0.0
    reason: str = ""


# Event callback signature: fn(event_type: str, payload: dict) -> None
EmitFn = Callable[[str, dict], None]


def crawl(
    start_url: str,
    emit: EmitFn,
    config: CrawlConfig | None = None,
    blocked: set[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    backtrack: list[dict] | None = None,
) -> CrawlResult:
    cfg = config or CrawlConfig()
    t0 = time.time()
    stop = should_stop or (lambda: False)

    # User-curated reverse graph: a page -> the page it links to on the way to
    # the goal. Reaching any of these "from" pages during the forward crawl
    # completes the route through the known (trusted) backward chain.
    backtrack_next: dict[str, tuple[str, str, str]] = {}
    for entry in (backtrack or []):
        f = normalize(entry.get("from", ""))
        t = normalize(entry.get("to", ""))
        if f and t and f != t:
            # Any non-empty type string is allowed (the UI only offers
            # click/search, but hidden paths may use a custom instruction).
            typ = (entry.get("type") or "click").strip() or "click"
            backtrack_next[f] = (t, (entry.get("text") or "").strip(), typ)

    # Normalize the user-supplied "cut" list so it matches discovered URLs.
    blocked_norm: set[str] = set()
    for b in (blocked or set()):
        nb = normalize(b)
        if nb:
            blocked_norm.add(nb)

    start = normalize(start_url)
    if start is None:
        emit("error", {"message": f"Not a crawlable http(s) URL: {start_url!r}"})
        return CrawlResult(found=False, reason="bad-start-url")

    goal_check = lambda u: is_goal(u, cfg.goal_hosts, cfg.match_subdomains,
                                   cfg.allow_subpages)

    parent: dict[str, str | None] = {start: None}
    incoming_text: dict[str, str] = {}  # url -> anchor text that led to it
    discovered: set[str] = {start}

    def reconstruct(goal: str) -> list[str]:
        chain = []
        node: str | None = goal
        while node is not None:
            chain.append(node)
            node = parent.get(node)
        chain.reverse()
        return chain

    def steps_for(chain: list[str], goal_text: str = "") -> list[dict]:
        # For each node, the link text clicked on the PREVIOUS page to reach it.
        # The goal's text is path-specific (a URL may be linked from many pages),
        # so it is passed in rather than read from the shared incoming_text map.
        steps = [{"url": u, "text": incoming_text.get(u, "")} for u in chain[:-1]]
        steps.append({"url": chain[-1], "text": goal_text})
        return steps

    def follow_backtrack(url: str) -> tuple[list[str], list[dict], bool]:
        """Walk the user's reverse chain from `url`; return (nodes, steps, reached).

        `nodes`/`steps` cover the hops *after* `url*, each marked bt=True.
        `reached` is True only if the chain actually ends at a goal — a known
        path that doesn't lead to the target is NOT a win.
        """
        nodes: list[str] = []
        steps: list[dict] = []
        guard = {url}
        cur = url
        reached = False
        while cur in backtrack_next:
            nxt, txt, typ = backtrack_next[cur]
            if cfg.clicks_only and typ != "click":
                break  # search/typing steps disabled — this route is unusable
            nodes.append(nxt)
            steps.append({"url": nxt, "text": txt or "(known link)",
                          "bt": True, "type": typ})
            if goal_check(nxt):
                reached = True
                break
            if nxt in guard:  # cycle without reaching a goal
                break
            guard.add(nxt)
            cur = nxt
        return nodes, steps, reached

    want = max(1, cfg.num_paths)

    # Paths are streamed as they're found, each with a unique running index, so
    # a known-path shortcut shows up instantly while the search keeps running to
    # find other (possibly faster) routes.
    found_seq = [0]
    seen_found: set[tuple] = set()
    emitted_chains: list[list[str]] = []

    def report(chain: list[str], steps: list[dict], kind: str = "static") -> None:
        key = tuple(chain)
        if key in seen_found:
            return
        seen_found.add(key)
        emit("found", {"kind": kind, "index": found_seq[0], "path": list(chain),
                       "steps": steps, "hops": len(chain) - 1})
        found_seq[0] += 1
        emitted_chains.append(list(chain))

    if goal_check(start):
        emit("node", {"url": start, "parent": None, "depth": 0})
        report([start], steps_for([start], ""))
        return CrawlResult(True, [start], 1, 0, time.time() - t0, "start-is-goal")

    emit("start", {"url": start})
    emit("node", {"url": start, "parent": None, "depth": 0})

    # If the start itself is a known backtrack page that reaches a goal, report
    # that route immediately — but keep crawling for other/faster paths.
    if start in backtrack_next:
        nodes, bsteps, reached = follow_backtrack(start)
        if reached:
            report([start] + nodes, [{"url": start, "text": ""}] + bsteps)

    # ------------------------------------------------------------------ #
    # "Find all shortest paths" mode: keep a full shortest-path DAG (every
    # predecessor at a node's BFS distance), then enumerate every distinct
    # path that achieves the minimum hop count.
    # ------------------------------------------------------------------ #
    if cfg.all_shortest:
        dist: dict[str, int] = {start: 0}
        preds: dict[str, list[str]] = {start: []}
        edge_text: dict[tuple, str] = {}
        all_disc: set[str] = {start}
        hits: list[tuple[str, str, str, bool]] = []  # (src, goal/bt url, text, is_bt)
        best: int | None = None
        frontier = [start]
        depth = 0

        while frontier and not stop():
            # If the shortest path this level could yield (depth+1) can't beat
            # what we already have, we're done.
            if best is not None and depth + 1 > best:
                break
            emit("level", {"depth": depth, "frontier": len(frontier)})
            next_set: list[str] = []
            seen_next: set[str] = set()
            with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
                futures = {pool.submit(fetch_links, u, cfg.timeout): u for u in frontier}
                for fut in as_completed(futures):
                    if stop():
                        break
                    src = futures[fut]
                    links = fut.result()[: cfg.per_page_links]
                    emit("visit", {"url": src, "depth": depth,
                                   "links": len(links), "visited": len(all_disc)})
                    nd = dist[src] + 1
                    for link, text in links:
                        if link in blocked_norm:
                            continue
                        is_direct = goal_check(link)
                        if not is_direct and link in backtrack_next:
                            bn, bs, reached = follow_backtrack(link)
                            if reached:
                                # Report the known route now (one thread, traced
                                # in full), then keep crawling this page like any
                                # other so faster/other paths are still found.
                                fwd: list[str] = []
                                cur: str | None = src
                                while cur is not None:
                                    fwd.append(cur)
                                    ps = preds.get(cur)
                                    cur = ps[0] if ps else None
                                fwd.reverse()
                                ksteps = [{"url": fwd[0], "text": ""}]
                                for i in range(1, len(fwd)):
                                    ksteps.append({"url": fwd[i],
                                                   "text": edge_text.get((fwd[i - 1], fwd[i]), "")})
                                ksteps.append({"url": link, "text": text})
                                ksteps += bs
                                report(fwd + [link] + bn, ksteps)
                            # fall through: treat `link` as a normal node/goal
                        if is_direct:
                            total = nd
                            if best is None or total <= best:
                                best = total if best is None else min(best, total)
                                hits.append((src, link, text, False))
                                edge_text[(src, link)] = text
                                emit("node", {"url": link, "parent": src, "text": text,
                                              "depth": nd, "goal": True, "backtrack": False})
                            continue
                        if link not in dist:
                            if len(all_disc) >= cfg.max_nodes:
                                continue
                            dist[link] = nd
                            preds[link] = [src]
                            edge_text[(src, link)] = text
                            all_disc.add(link)
                            if link not in seen_next:
                                seen_next.add(link)
                                next_set.append(link)
                            emit("node", {"url": link, "parent": src,
                                          "text": text, "depth": nd})
                        elif dist[link] == nd:
                            if src not in preds[link]:
                                preds[link].append(src)
                            edge_text.setdefault((src, link), text)
            frontier = next_set
            depth += 1

        # DAG helpers (for enumeration + JS verification).
        PER_NODE_CAP = 400
        memo: dict[str, list[list[str]]] = {}

        def paths_to(node: str) -> list[list[str]]:
            if node in memo:
                return memo[node]
            ps = preds.get(node) or []
            if not ps:
                memo[node] = [[node]]
                return memo[node]
            out: list[list[str]] = []
            for p in ps:
                for sub in paths_to(p):
                    out.append(sub + [node])
                    if len(out) >= PER_NODE_CAP:
                        break
                if len(out) >= PER_NODE_CAP:
                    break
            memo[node] = out
            return out

        def steps_from_nodes(nodes: list[str]) -> list[dict]:
            st = [{"url": nodes[0], "text": ""}]
            for i in range(1, len(nodes)):
                st.append({"url": nodes[i],
                           "text": edge_text.get((nodes[i - 1], nodes[i]), "")})
            return st

        def reconstruct_short(node: str) -> list[str]:
            ch = [node]
            cur = node
            while preds.get(cur):
                cur = preds[cur][0]
                ch.append(cur)
            ch.reverse()
            return ch

        def steps_for_dag(chain: list[str], goal_text: str = "") -> list[dict]:
            st = steps_from_nodes(list(chain))
            if st:
                st[-1]["text"] = goal_text or st[-1]["text"]
            return st

        # Enumerate every shortest DIRECT path (those with the minimum steps).
        capped = False
        if best is not None:
            TOTAL_CAP = 1000
            seen_full: set[tuple] = set()
            count = 0
            for src, link, text, _is_bt in hits:
                if dist[src] + 1 != best:
                    continue
                for base in paths_to(src):
                    chain = tuple(base + [link])
                    if chain in seen_full:
                        continue
                    seen_full.add(chain)
                    report(list(chain), steps_from_nodes(list(chain)))
                    count += 1
                    if count >= TOTAL_CAP:
                        capped = True
                        break
                if capped:
                    break
            if capped:
                emit("jsnote", {"message": f"Showing the first {TOTAL_CAP} shortest "
                                f"paths ({best} steps) — there are more."})

        if not emitted_chains:
            emit("exhausted", {"visited": len(all_disc), "depth": cfg.max_depth})
            return CrawlResult(False, [], len(all_disc), cfg.max_depth,
                               time.time() - t0, "no-path-within-limits")

        if cfg.verify_js and not stop():
            js_verify(list(emitted_chains), emit, cfg, goal_check,
                      blocked_norm, reconstruct_short, steps_for_dag, stop)

        first = emitted_chains[0]
        return CrawlResult(True, first, len(all_disc), len(first) - 1,
                           time.time() - t0, f"all-shortest-{len(emitted_chains)}-paths")

    # Phase 1 — gather candidate goal-paths (BFS, so shortest-first). We keep
    # collecting until we have enough *distinct first hops* (links off the start)
    # to satisfy the request, or the search is exhausted.
    cand_cap = max(200, want * 20)
    candidates: list[tuple[tuple, list]] = []   # (chain, steps)
    seen_chains: set[tuple] = set()
    first_hops: set[str] = set()

    frontier = [start]
    last_depth = 0

    for depth in range(cfg.max_depth):
        if (not frontier or len(first_hops) >= want
                or len(candidates) >= cand_cap or stop()):
            break
        last_depth = depth + 1
        emit("level", {"depth": depth, "frontier": len(frontier)})

        next_frontier: list[str] = []
        enough = False

        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            futures = {
                pool.submit(fetch_links, url, cfg.timeout): url
                for url in frontier
            }
            for fut in as_completed(futures):
                if stop():
                    enough = True  # reuse the early-exit path to cancel futures
                    break
                src = futures[fut]
                links = fut.result()[: cfg.per_page_links]
                emit("visit", {
                    "url": src,
                    "depth": depth,
                    "links": len(links),
                    "visited": len(discovered),
                })

                for link, text in links:
                    if link in blocked_norm:
                        continue  # user cut this link (e.g. it was hidden)
                    is_direct = goal_check(link)
                    if not is_direct and link in backtrack_next:
                        nodes_after, bsteps, reached = follow_backtrack(link)
                        if reached:
                            # Report the known route now (one thread, traced in
                            # full), then keep crawling this page for other paths.
                            pre = reconstruct(src)
                            ksteps = [{"url": n, "text": incoming_text.get(n, "")}
                                      for n in pre]
                            ksteps.append({"url": link, "text": text})
                            ksteps += bsteps
                            report(pre + [link] + nodes_after, ksteps)
                        # fall through: treat `link` as a normal node/goal
                    if is_direct:
                        # Build this path directly from src so routes through
                        # different start-links each get their own chain.
                        pre = reconstruct(src)
                        steps = [{"url": n, "text": incoming_text.get(n, "")}
                                 for n in pre]
                        steps.append({"url": link, "text": text})
                        chain = tuple(pre + [link])
                        if chain not in seen_chains and len(candidates) < cand_cap:
                            seen_chains.add(chain)
                            candidates.append((chain, steps))
                            first_hops.add(chain[1])
                            emit("node", {"url": link, "parent": src, "text": text,
                                          "depth": depth + 1, "goal": True,
                                          "backtrack": False})
                            if len(first_hops) >= want:
                                enough = True
                                break
                        continue  # never expand a goal node
                    if link not in discovered and len(discovered) < cfg.max_nodes:
                        # `discovered` guarantees each link is emitted only once,
                        # so the tree never shows duplicates.
                        discovered.add(link)
                        parent[link] = src
                        incoming_text[link] = text
                        next_frontier.append(link)
                        emit("node", {"url": link, "parent": src, "text": text,
                                      "depth": depth + 1})

                if enough:
                    break

            if enough:
                for f in futures:
                    f.cancel()

        frontier = next_frontier

    # Phase 2 — pick `want` direct paths (root-diverse) and report them; any
    # known-path routes were already streamed during the crawl.
    for idx in select_diverse(candidates, want):
        chain, steps = candidates[idx]
        report(list(chain), steps)

    if not emitted_chains:
        emit("exhausted", {"visited": len(discovered), "depth": cfg.max_depth})
        return CrawlResult(
            False, [], len(discovered), cfg.max_depth,
            time.time() - t0, "no-path-within-limits",
        )

    # Phase 3 — JavaScript verification over the pages in the found paths.
    if cfg.verify_js and not stop():
        js_verify(list(emitted_chains), emit, cfg, goal_check, blocked_norm,
                  reconstruct, steps_for, stop)

    first = emitted_chains[0]
    return CrawlResult(
        True, first, len(discovered), last_depth,
        time.time() - t0, f"found-{len(emitted_chains)}-paths",
    )


def js_verify(selected_chains, emit, cfg, goal_check, blocked_norm,
              reconstruct, steps_for, stop=None) -> None:
    """Render path pages in a browser and report JS-only goal routes."""
    import jsrender
    stop = stop or (lambda: False)

    # Every page you'd actually visit along the found paths (exclude the goal
    # nodes themselves), de-duplicated, start-first.
    pages: list[str] = []
    seen_pages: set[str] = set()
    for chain in selected_chains:
        for node in chain[:-1]:
            if node not in seen_pages:
                seen_pages.add(node)
                pages.append(node)
    pages = pages[: cfg.js_max_pages]

    emit("jsphase", {"pages": len(pages),
                     "available": jsrender.playwright_available()})
    if not pages:
        emit("jsdone", {"count": 0})
        return
    if not jsrender.playwright_available():
        emit("jsnote", {"message": "Playwright not installed — run "
                        "'playwright install chromium' to enable JS verification."})
        emit("jsdone", {"count": 0})
        return

    def progress(url, ok, n):
        emit("jsrender", {"url": url, "ok": ok, "links": n})

    try:
        rendered = jsrender.render_pages_links(pages, cfg.timeout, progress, stop)
    except Exception as exc:
        emit("jsnote", {"message": f"JS rendering failed: {exc}"})
        emit("jsdone", {"count": 0})
        return

    # For each page, find rendered goal links that the STATIC fetch missed.
    js_candidates: list[tuple[tuple, str]] = []
    seen_chains: set[tuple] = set()
    for page in pages:
        rlinks = rendered.get(page, [])
        rendered_goals = []
        for href, text in rlinks:
            nu = normalize(href)
            if nu and nu not in blocked_norm and goal_check(nu):
                rendered_goals.append((nu, text))
        if not rendered_goals:
            continue
        static_set = {su for su, _ in fetch_links(page, cfg.timeout)}
        for g, text in rendered_goals:
            if g in static_set:
                continue  # was visible statically too — not a JS-only find
            chain = tuple(reconstruct(page) + [g])
            if chain not in seen_chains:
                seen_chains.add(chain)
                js_candidates.append((chain, text))

    if not js_candidates:
        emit("jsdone", {"count": 0})
        return

    # Keep it "close to as many as the normal paths": cap at that count, and
    # prefer root-diverse / shortest ones.
    want_js = max(1, len(selected_chains))
    for i, idx in enumerate(select_diverse(js_candidates, want_js)):
        chain, goal_text = js_candidates[idx]
        chain = list(chain)
        emit("found", {
            "kind": "js",
            "index": i,
            "path": chain,
            "steps": steps_for(chain, goal_text),
            "hops": len(chain) - 1,
        })
    emit("jsdone", {"count": min(want_js, len(js_candidates))})


def select_diverse(candidates: list[tuple[tuple, str]], want: int) -> list[int]:
    """Pick up to `want` candidate indices maximizing root diversity.

    Each pick is the candidate whose shallowest edge not yet used by an
    already-picked path is as shallow as possible (ties: shorter path, then
    earlier discovery). This makes successive paths branch from a new link on
    the start first, dropping to deeper links only when shallower ones run out.
    """
    used_prefixes: set[tuple] = set()
    pool = list(range(len(candidates)))
    selected: list[int] = []

    def shallowest_new_edge(chain: tuple) -> int:
        for k in range(1, len(chain)):
            if chain[: k + 1] not in used_prefixes:
                return k
        return len(chain)  # fully subsumed (shouldn't happen: chains are unique)

    while pool and len(selected) < want:
        best_pos = min(
            range(len(pool)),
            key=lambda pos: (
                shallowest_new_edge(candidates[pool[pos]][0]),
                len(candidates[pool[pos]][0]),
                pool[pos],
            ),
        )
        idx = pool.pop(best_pos)
        chain = candidates[idx][0]
        for k in range(1, len(chain)):
            used_prefixes.add(chain[: k + 1])
        selected.append(idx)

    return selected
