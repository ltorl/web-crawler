"""
Optional JavaScript-rendering helper built on Playwright.

The static crawler (requests + BeautifulSoup) can't see links that a page
injects with JavaScript — e.g. GitHub's React app-header home button, or the
icon links on client-rendered SPAs. This module renders a batch of pages in a
real headless browser and returns the links present in the *rendered* DOM, so
the crawler can verify whether a shorter/alternative path exists through links
it couldn't see statically.

Everything here is best-effort and fully optional: if Playwright or its browser
isn't installed, `playwright_available()` returns False and the caller skips
the whole JS phase.
"""

from __future__ import annotations

from typing import Callable

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Extract every rendered anchor's absolute href + its accessible-ish label.
_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => {
  let text = (a.innerText || '').trim();
  if (!text) text = (a.getAttribute('aria-label') || '').trim();
  if (!text) {
    const svg = a.querySelector('svg');
    if (svg) {
      text = (svg.getAttribute('aria-label') || '').trim();
      if (!text) {
        const t = svg.querySelector('title');
        if (t) text = t.textContent.trim();
      }
      if (!text) {
        const cls = Array.from(svg.classList).find(c => c.startsWith('octicon-'));
        if (cls) {
          const n = cls.slice('octicon-'.length);
          text = (n === 'mark-github' || n === 'logo-github') ? 'GitHub' : n.replace(/-/g, ' ');
        }
      }
    }
  }
  if (!text) {
    const img = a.querySelector('img[alt]');
    if (img) text = (img.getAttribute('alt') || '').trim();
  }
  return { href: a.href, text: text.slice(0, 140) };
});
"""


def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False
    return True


# Progress callback: fn(url: str, ok: bool, n_links: int) -> None
ProgressFn = Callable[[str, bool, int], None]


def render_pages_links(
    urls: list[str],
    timeout: float = 12.0,
    progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Render each URL in a headless browser; return {url: [(href, text), ...]}.

    Pages are rendered sequentially in one browser instance. Failures (timeouts,
    nav errors) yield an empty link list for that URL rather than raising. If
    `should_stop()` becomes true, rendering stops early.
    """
    from playwright.sync_api import sync_playwright

    stop = should_stop or (lambda: False)
    results: dict[str, list[tuple[str, str]]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        for url in urls:
            if stop():
                break
            page = context.new_page()
            links: list[tuple[str, str]] = []
            ok = False
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # Give client-side frameworks a moment to inject content.
                page.wait_for_timeout(1500)
                raw = page.evaluate(_EXTRACT_JS)
                seen: set[str] = set()
                for item in raw:
                    href = item.get("href") or ""
                    if href and href not in seen:
                        seen.add(href)
                        links.append((href, item.get("text") or ""))
                ok = True
            except Exception:
                links = []
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            results[url] = links
            if progress:
                progress(url, ok, len(links))
        context.close()
        browser.close()
    return results
