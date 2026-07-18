#!/usr/bin/env python3
"""Internet search routed through Tor (SOCKS5 at 127.0.0.1:9050).

Used by the build agent to pull live web context before building. Also runnable
standalone:  python3 tor_search.py "your query"

Tor gives the searches a rotating exit IP rather than the VM's own address.
Falls back gracefully (returns []) if Tor or the network is unavailable so a
build is never blocked by a search failure.
"""
import sys

import requests
from bs4 import BeautifulSoup

TOR_PROXIES = {
    "http": "socks5h://127.0.0.1:9050",
    "https": "socks5h://127.0.0.1:9050",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) code-builder/1.0"}
DDG_HTML = "https://html.duckduckgo.com/html/"


def search(query: str, max_results: int = 5, timeout: int = 30) -> list[dict]:
    """Return [{'title','url','snippet'}, ...] for a query, via Tor."""
    try:
        resp = requests.post(
            DDG_HTML,
            data={"q": query},
            headers=HEADERS,
            proxies=TOR_PROXIES,
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"[tor_search] search failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[dict] = []
    for res in soup.select(".result"):
        a = res.select_one(".result__a")
        if not a:
            continue
        snippet_el = res.select_one(".result__snippet")
        results.append(
            {
                "title": a.get_text(strip=True),
                "url": a.get("href", ""),
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            }
        )
        if len(results) >= max_results:
            break
    return results


def format_findings(query: str, results: list[dict]) -> str:
    if not results:
        return f"### Search: {query}\n(no results)\n"
    lines = [f"### Search: {query}"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "hello world"
    print(format_findings(q, search(q)))
