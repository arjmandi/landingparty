"""Pinned paid-search protocols; no agent-selected endpoints or extra content calls."""

from datetime import datetime, timedelta

import httpx

PROVIDERS = {
    "brave": {
        "id": "brave@v1",
        "key_env": "WORLD_BRAVE_API_KEY",
        "header": "x-subscription-token",
        "method": "GET",
        "url": "https://api.search.brave.com/res/v1/web/search",
        "price_key": "search_usd_per_call",
    },
    "exa": {
        "id": "exa@search-v1/neural",
        "key_env": "WORLD_EXA_API_KEY",
        "header": "x-api-key",
        "method": "POST",
        "url": "https://api.exa.ai/search",
        "price_key": "exa_search_usd_per_call",
    },
}


def search_price(provider):
    from .lane import WORLD_TOOL_PRICES

    return WORLD_TOOL_PRICES[PROVIDERS[provider]["price_key"]]


def provider_request(provider, api_key, params, *, submitted_at):
    spec = PROVIDERS[provider]
    headers = {spec["header"]: api_key, "Accept": "application/json"}
    query, k = params["query"], params.get("k", 10)
    site, freshness = params.get("site"), params.get("freshness")
    if provider == "brave":
        arguments = {"q": f"{query} site:{site}" if site else query, "count": k}
        if freshness:
            arguments["freshness"] = freshness
        return httpx.Request(spec["method"], spec["url"], params=arguments, headers=headers)
    # A search-only neural request never requests separately priced contents,
    # summaries, deep search, or answer generation. Time filters are frozen at
    # the world's action timestamp so the boundary can reconstruct the body.
    arguments = {"query": query, "numResults": k, "type": "neural"}
    if site:
        arguments["includeDomains"] = [site]
    if freshness:
        instant = datetime.fromisoformat(submitted_at)
        days = {"pd": 1, "pw": 7, "pm": 30, "py": 365}[freshness]
        arguments["startPublishedDate"] = (instant - timedelta(days=days)).isoformat()
    return httpx.Request(spec["method"], spec["url"], json=arguments, headers=headers)


def normalize_results(provider, payload, k):
    if not isinstance(payload, dict):
        raise TypeError("Search provider response must be an object")
    container = payload.get("web", {}) if provider == "brave" else payload
    if not isinstance(container, dict):
        raise TypeError("Search result container must be an object")
    results = container.get("results", [])
    if not isinstance(results, list):
        raise TypeError("Search provider results must be a list")
    items = []
    for i, item in enumerate(results[:k]):
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise TypeError("Search result has no URL")
        title = item.get("title") or ""
        snippet = item.get("description", "") if provider == "brave" else item.get("summary", "")
        if not isinstance(title, str) or not isinstance(snippet, str):
            raise TypeError("Search result text is malformed")
        published = item.get("page_age" if provider == "brave" else "publishedDate")
        items.append(
            {
                "rank": i + 1,
                "url": item["url"],
                "title": title,
                "snippet": snippet,
                **({"published": published} if isinstance(published, str) else {}),
            }
        )
    return {"kind": "result", "provider_id": PROVIDERS[provider]["id"], "items": items}
