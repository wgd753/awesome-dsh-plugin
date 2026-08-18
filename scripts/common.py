"""GitHub API helpers for dsh-catalog aggregation.

Handles: auth via GITHUB_TOKEN, rate-limit awareness, exponential backoff on
secondary rate limits, search-result pagination (with the 1000-result cap
worked around by window splitting in aggregate.py).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = "dsh-catalog/1.0 (github auto aggregation)"

TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _headers():
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _read_limits(resp):
    return {
        "remaining": resp.headers.get("x-ratelimit-remaining"),
        "limit": resp.headers.get("x-ratelimit-limit"),
        "reset": int(resp.headers.get("x-ratelimit-reset") or 0),
    }


def gh_get(path, params=None, tries=5):
    """GET an API path with retry/backoff on rate limits. Returns parsed JSON."""
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 403:  # rate limited (or forbidden)
                retry_after = e.headers.get("retry-after")
                reset = e.headers.get("x-ratelimit-reset")
                delay = int(retry_after) if retry_after else None
                if not delay and reset:
                    delay = max(1, int(reset) - int(time.time()))
                delay = delay or (30 * attempt)
                print(f"  [rate-limit] 403 on {path}; sleeping {delay}s (try {attempt}/{tries})", flush=True)
                time.sleep(delay)
                continue
            if e.code in (429, 500, 502, 503):
                time.sleep(20 * attempt)
                continue
            raise
        except urllib.error.URLError as e:
            time.sleep(15 * attempt)
            continue
    raise RuntimeError(f"persistent failure fetching {path}")


def search_repos(query, page=1, per_page=100):
    """One page of /search/repositories. Returns dict with total_count + items."""
    params = {
        "q": query,
        "sort": "stars", "order": "desc",
        "per_page": per_page, "page": page,
    }
    return gh_get("/search/repositories", params=params)


def slim_repo(r):
    """Reduce search-result repo dict to portable fields."""
    stars = r.get("stargazers_count") or 0
    topics = r.get("topics") or []
    return {
        "full_name": r["full_name"],
        "html_url": r.get("html_url") or f"https://github.com/{r['full_name']}",
        "homepage": r.get("homepage") or "",
        "description": (r.get("description") or "").strip(),
        "stars": stars,
        "forks": r.get("forks_count") or 0,
        "language": r.get("language"),
        "license": (r.get("license") or {}).get("spdx_id") if isinstance(r.get("license"), dict) else None,
        "archived": bool(r.get("archived")),
        "disabled": bool(r.get("disabled")),
        "fork": bool(r.get("fork")),
        "created_at": r.get("created_at", "")[:10],
        "pushed_at": r.get("pushed_at", "")[:10],
        "topics": topics,
        "default_branch": r.get("default_branch"),
    }


def fetch_search_full(query, sleep_sec=2.1):
    """Fetch ALL results of a search query (respecting the 1000 cap).

    Optionally split long windows first (see aggregate.py). Yields slim dicts.
    Uses authenticated quota when GITHUB_TOKEN is set (30 req/min -> ~3x
    unauthenticated), so sleeps hard for safety.
    """
    per = 100
    d = search_repos(query, page=1, per_page=per)
    total = d.get("total_count", 0)
    items = d.get("items", [])
    print(f"  query '{query}' total={total}", flush=True)
    for r in items:
        yield slim_repo(r)
    pages = max(1, (min(total, 1000) + per - 1) // per)
    for page in range(2, pages + 1):
        time.sleep(sleep_sec)
        d = search_repos(query, page=page, per_page=per)
        for r in d.get("items", []):
            yield slim_repo(r)
        if len(d.get("items", [])) < per:
            break