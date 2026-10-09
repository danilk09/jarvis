"""
Web lookups: Brave Search plus a short spoken summary from Claude.
"""

import re

import requests

from . import config, verbosity


def brave_search(query, count=5):
    """Call Brave Search API and return a list of {title, description, url} dicts."""
    if not config.BRAVE_API_KEY:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": config.BRAVE_API_KEY},
            params={"q": query, "count": count},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [{"title": r.get("title", ""), "description": r.get("description", ""), "url": r.get("url", "")}
                for r in results]
    except Exception as e:
        print(f"  Brave search error: {e}")
        return []


def synthesize_search_answer(query, results):
    """Ask Claude to turn raw search snippets into a short spoken answer."""
    if not results:
        return "I couldn't find anything for that. Try again or check your Brave API key."
    snippets = "\n".join(f"{i+1}. {r['title']}: {r['description']} ({r['url']})"
                         for i, r in enumerate(results))
    try:
        response = config.client.messages.create(
            model=config.MODEL,
            max_tokens=verbosity.tokens(200, 600),
            system="You are a voice assistant. Using the search results below, answer the question. "
                   + verbosity.rule() + " No markdown, no bullet points — plain conversational sentences only.",
            messages=[{"role": "user", "content": f"Question: {query}\n\nSearch results:\n{snippets}"}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"Search succeeded but summary failed: {e}"


def fetch_text(url, limit=4000):
    """Download a page and return its visible text, roughly."""
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
    clean = re.sub(r"<[^>]+>", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()[:limit]
