"""Web research connector — search + page reading (keyless).

Serves the "research & decisions" delegation from the 2026-06 needs interview:
Aria previously could not look ANYTHING up online. DuckDuckGo for search (no
API key), requests+BeautifulSoup for reading pages.
"""
import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_MAX_PAGE_CHARS = 8000


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Use for anything needing current information, lookups,
    comparisons, prices, opening hours, news — research is one of your duties;
    don't answer from stale knowledge when a search would do better. Follow up
    with fetch_webpage on promising results for detail."""
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=max_results)
    except Exception as e:
        return f"Search failed: {e}"
    if not results:
        return "No results."
    out = []
    for r in results:
        out.append(f"- {r.get('title', '?')}\n  {r.get('href', '')}\n  {r.get('body', '')[:200]}")
    return "\n".join(out)


@tool
def fetch_webpage(url: str) -> str:
    """Read a webpage's text content (use after web_search to go deeper on a
    result, or when the user shares a link)."""
    try:
        resp = requests.get(url, headers={'User-Agent': _UA}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = ' '.join(soup.get_text(separator=' ').split())
        if len(text) > _MAX_PAGE_CHARS:
            text = text[:_MAX_PAGE_CHARS] + " ...[truncated]"
        return text or "Page had no readable text."
    except Exception as e:
        return f"Couldn't fetch that page: {e}"
