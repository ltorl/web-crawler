"""
Web app: drop a URL in the middle of the page and we dispatch concurrent
crawlers that breadth-first their way to github.com, then draw the shortest
hop-path back.

Backend: Flask. The crawl runs in a background thread that pushes progress
events onto a queue; the browser consumes them over Server-Sent Events so the
search animates live.
"""

from __future__ import annotations

import json
import queue
import re
import threading

from flask import Flask, Response, render_template, request, stream_with_context

from crawler import GOAL_HOSTS, CrawlConfig, crawl

app = Flask(__name__)


def parse_goal_hosts(raw: str) -> tuple:
    """Turn a user "custom ending" string into a tuple of clean hosts.

    Accepts comma/space/newline-separated entries, with or without scheme,
    'www.', path, or port, e.g. "https://www.wikipedia.org/, reddit.com".
    Falls back to the default goal hosts when nothing usable is given.
    """
    hosts: list[str] = []
    for tok in re.split(r"[\s,]+", raw or ""):
        tok = tok.strip().lower()
        if not tok:
            continue
        tok = re.sub(r"^[a-z]+://", "", tok)  # drop scheme
        tok = tok.split("/")[0].split(":")[0]  # drop path + port
        if tok.startswith("www."):
            tok = tok[4:]
        if tok and "." in tok and tok not in hosts:
            hosts.append(tok)
    return tuple(hosts) if hosts else GOAL_HOSTS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/crawl")
def crawl_stream():
    start_url = (request.args.get("url") or "").strip()
    if start_url and "://" not in start_url:
        start_url = "https://" + start_url

    # Optional tuning knobs from the UI.
    def _int(name, default):
        try:
            return max(1, int(request.args.get(name, default)))
        except (TypeError, ValueError):
            return default

    def _float(name, default):
        try:
            return max(0.5, float(request.args.get(name, default)))
        except (TypeError, ValueError):
            return default

    def _limit(name, default):
        # The UI sends "0" when the user turns the limit off — treat as no cap.
        if request.args.get(name) == "0":
            return 10 ** 9
        return _int(name, default)

    goal_hosts = parse_goal_hosts(request.args.get("goal", ""))

    config = CrawlConfig(
        workers=_int("workers", 16),
        max_depth=_int("depth", 6),
        max_nodes=_limit("maxnodes", 4000),
        per_page_links=_limit("perpage", 40),
        timeout=_float("timeout", 8.0),
        goal_hosts=goal_hosts,
        match_subdomains=request.args.get("subdomains") == "1",
        allow_subpages=request.args.get("subpages", "1") != "0",
        num_paths=min(50, _int("paths", 1)),
        all_shortest=request.args.get("allshortest", "1") != "0",
        clicks_only=request.args.get("clicksonly") == "1",
        verify_js=request.args.get("js", "1") != "0",
    )

    # "Cut" list: URLs the crawler must not follow (e.g. hidden links). The UI
    # sends them newline-separated; we also accept repeated ?block= params.
    blocked: set[str] = set()
    raw_block = request.args.get("block", "")
    for line in raw_block.replace(",", "\n").splitlines():
        line = line.strip()
        if line:
            blocked.add(line)
    blocked.update(b.strip() for b in request.args.getlist("block") if b.strip())

    # Backtrack: user-curated reverse links toward the goal, as a JSON array of
    # {"from": url, "to": url, "text": label}.
    backtrack: list[dict] = []
    raw_bt = request.args.get("bt", "")
    if raw_bt:
        try:
            parsed = json.loads(raw_bt)
            if isinstance(parsed, list):
                backtrack = [e for e in parsed if isinstance(e, dict)]
        except (ValueError, TypeError):
            backtrack = []

    # Hidden known paths: always present, never shown in the UI. <target url>
    # resolves to the goal. Two curated routes:
    #  - From Google: search for a URL opener, open it, type the target in.
    #  - From GitHub: search for altior-browser and open the project's site.
    target_url = "https://" + goal_hosts[0] + "/"
    hidden_backtrack = [
        {"from": "https://www.google.com/",
         "to": "https://www.google.com/search?q=url+opener",
         "type": "search", "text": "url opener"},
        {"from": "https://www.google.com/search?q=url+opener",
         "to": "https://url-opener.com/",
         "type": "click", "text": "Url Opener"},
        {"from": "https://url-opener.com/", "to": target_url,
         "type": "Type ___ and Click Enter", "text": target_url},
        {"from": "https://github.com",
         "to": "https://github.com/search?q=altior-browser",
         "type": "search", "text": "altior-browser"},
        {"from": "https://github.com/search?q=altior-browser",
         "to": "https://github.com/ltorl/altior-browser",
         "type": "click", "text": "ltorl/altior-browser"},
        {"from": "https://github.com/ltorl/altior-browser",
         "to": "https://altior-browser.onrender.com/",
         "type": "click", "text": "https://altior-browser.onrender.com/"},
    ]
    # Hidden first so an explicit user entry for the same page takes precedence.
    backtrack = hidden_backtrack + backtrack

    events: "queue.Queue[tuple[str, dict] | None]" = queue.Queue()
    cancel = threading.Event()  # set when the client disconnects (Stop button)

    def emit(event_type: str, payload: dict) -> None:
        events.put((event_type, payload))

    def worker():
        try:
            if not start_url:
                emit("error", {"message": "No URL provided."})
            else:
                crawl(start_url, emit, config, blocked=blocked,
                      should_stop=cancel.is_set, backtrack=backtrack)
        except Exception as exc:  # never let the stream hang on a crash
            emit("error", {"message": f"Crawler crashed: {exc}"})
        finally:
            events.put(None)  # sentinel: stream complete

    threading.Thread(target=worker, daemon=True).start()

    @stream_with_context
    def generate():
        # Tell proxies not to buffer the stream.
        yield ": connected\n\n"
        try:
            while True:
                item = events.get()
                if item is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                event_type, payload = item
                yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
        finally:
            # Client went away (Stop pressed / tab closed) — tell the crawler.
            cancel.set()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


if __name__ == "__main__":
    import os
    # Port 5000 is hijacked by AirPlay Receiver on macOS, so default to 5050.
    port = int(os.environ.get("PORT", "5050"))
    print(f"GitHub Path Crawler running on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
