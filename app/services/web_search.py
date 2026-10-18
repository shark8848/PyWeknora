"""Web search provider adapters used by tests and agent tools."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from .. import db
from ..utils import loads
from . import url_guard


class WebSearchError(RuntimeError):
    pass


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            url_guard.validate_url_for_ssrf(newurl)
        except url_guard.URLGuardError as exc:
            raise WebSearchError(f"Search redirect blocked: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20,
    proxy_url: str = "",
) -> tuple[int, bytes]:
    try:
        url_guard.validate_url_for_ssrf(url)
    except url_guard.URLGuardError as exc:
        raise WebSearchError(f"Search request blocked: {exc}") from exc
    proxy = str(proxy_url or "").strip()
    if proxy:
        try:
            url_guard.validate_proxy_url(proxy)
        except url_guard.URLGuardError as exc:
            raise WebSearchError(f"Invalid proxy_url: {exc}") from exc
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PyWeKnora/1.0)",
        **({"Content-Type": "application/json"} if data is not None else {}),
        **(headers or {}),
    }
    if not proxy:
        try:
            status, _, response_body, _ = url_guard.request_url(
                method,
                url,
                headers=request_headers,
                body=data,
                timeout=timeout,
            )
            return status, response_body
        except url_guard.URLGuardError as exc:
            raise WebSearchError(f"Search request failed: {exc}") from exc
        except (TimeoutError, OSError) as exc:
            raise WebSearchError(f"Search request failed: {exc}") from exc
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    handlers: list[Any] = [_SafeRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WebSearchError(f"Search request failed: {exc}") from exc


def _search_http(
    parameters: dict[str, Any],
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20,
) -> tuple[int, bytes]:
    proxy = str(parameters.get("proxy_url") or "").strip()
    return _http(
        method,
        url,
        headers=headers,
        body=body,
        timeout=timeout,
        **({"proxy_url": proxy} if proxy else {}),
    )


def _json_response(status: int, body: bytes, provider: str) -> dict[str, Any]:
    if status >= 400:
        raise WebSearchError(f"{provider} returned HTTP {status}: {body.decode('utf-8', errors='replace')[:300]}")
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise WebSearchError(f"{provider} returned invalid JSON") from exc
    return value if isinstance(value, dict) else {}


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = values.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": values.get("href", ""), "content": ""}
            self._capture = "title"
            self._text = []
        elif tag == "a" and "result__snippet" in classes and self._current is not None:
            self._capture = "content"
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capture or self._current is None:
            return
        text = " ".join("".join(self._text).split())
        if self._capture == "title" and text:
            self._current["title"] = text
        elif self._capture == "content" and text:
            self._current["content"] = text
            self.results.append(self._current)
            self._current = None
        self._capture = ""
        self._text = []


def _decode_ddg_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
    return urllib.parse.unquote(target) if target else value


def _duckduckgo(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    status, body = _search_http(parameters, "GET", url)
    if status >= 400:
        raise WebSearchError(f"duckduckgo returned HTTP {status}")
    parser = _DuckDuckGoParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    results = []
    for item in parser.results[:limit]:
        results.append(
            {
                "title": html.unescape(item.get("title") or ""),
                "url": _decode_ddg_url(item.get("url") or ""),
                "content": html.unescape(item.get("content") or ""),
                "source": "duckduckgo",
            }
        )
    return results


def _searxng(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    base = str(parameters.get("base_url") or parameters.get("url") or "").rstrip("/")
    if not base:
        raise WebSearchError("base_url is required for SearXNG")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebSearchError("invalid SearXNG base_url: must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise WebSearchError("invalid SearXNG base_url: must not contain query or fragment")
    url = base + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "language": "all"}
    )
    status, body = _search_http(
        parameters,
        "GET",
        url,
        headers={"Accept": "application/json", "User-Agent": "WeKnora/1.0"},
        timeout=12,
    )
    payload = _json_response(status, body, "searxng")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("content") or ""),
            "published_date": str(item.get("publishedDate") or ""),
            "source": "searxng",
        }
        for item in (payload.get("results") or [])[:limit]
        if isinstance(item, dict) and item.get("title") and item.get("url")
    ]


def _api_key(parameters: dict[str, Any]) -> str:
    credentials = parameters.get("_credentials") if isinstance(parameters.get("_credentials"), dict) else {}
    return str(parameters.get("api_key") or credentials.get("api_key") or "")


def _extra_config(parameters: dict[str, Any]) -> dict[str, Any]:
    extra = parameters.get("extra_config")
    return extra if isinstance(extra, dict) else {}


def _truncate_width(value: str, max_width: int) -> str:
    chars: list[str] = []
    width = 0
    for char in value:
        char_width = 1 if ord(char) < 128 else 2
        if width + char_width > max_width:
            break
        chars.append(char)
        width += char_width
    return "".join(chars)


def _validate_searxng_base(base: str) -> None:
    parsed = urllib.parse.urlsplit(base)
    if not base:
        raise WebSearchError("base_url is required for SearXNG")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebSearchError("invalid SearXNG base_url: must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise WebSearchError("invalid SearXNG base_url: must not contain query or fragment")
    try:
        url_guard.validate_url_for_ssrf(base)
    except url_guard.URLGuardError as exc:
        raise WebSearchError(f"invalid SearXNG base_url: {exc}") from exc


def _validate_parameters(provider_type: str, parameters: dict[str, Any]) -> None:
    key_required = {"brave", "serply", "bing", "tavily", "ollama", "baidu", "exa"}
    if provider_type in key_required and not _api_key(parameters):
        raise WebSearchError(f"API key is required for {provider_type.capitalize()}")
    if provider_type == "google":
        if not _api_key(parameters):
            raise WebSearchError("API key is required for Google")
        if not str(parameters.get("engine_id") or parameters.get("cx") or "").strip():
            raise WebSearchError("engine ID is required for Google")
    extra = _extra_config(parameters)
    if provider_type == "metaso":
        if not _api_key(parameters):
            raise WebSearchError("API key is required for Metaso")
        scope = str(extra.get("scope") or "webpage").strip()
        if scope not in _METASO_SCOPES:
            raise WebSearchError(f"invalid Metaso search scope: {scope}")
    elif provider_type == "zhipu":
        if not _api_key(parameters):
            raise WebSearchError("API key is required for Zhipu")
        engine = str(extra.get("search_engine") or "search_std").strip()
        if engine not in {"search_std", "search_pro", "search_pro_sogou", "search_pro_quark"}:
            raise WebSearchError(f"invalid Zhipu search engine: {engine}")
        content_size = str(extra.get("content_size") or "medium").strip()
        if content_size not in {"medium", "high"}:
            raise WebSearchError(f"invalid Zhipu content size: {content_size}")
    elif provider_type == "bocha":
        if not _api_key(parameters):
            raise WebSearchError("API key is required for Bocha")
        freshness = str(extra.get("freshness") or "noLimit").strip()
        if freshness not in {"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}:
            raise WebSearchError(f"invalid Bocha freshness: {freshness}")
    elif provider_type == "searxng":
        _validate_searxng_base(str(parameters.get("base_url") or parameters.get("url") or "").strip())
    proxy = str(parameters.get("proxy_url") or "").strip()
    if proxy:
        try:
            url_guard.validate_proxy_url(proxy)
        except url_guard.URLGuardError as exc:
            raise WebSearchError(f"invalid proxy_url: {exc}") from exc


def validate_parameters(provider: str, parameters: Any) -> None:
    provider_type = str(provider or "duckduckgo").strip().lower()
    if provider_type not in PROVIDERS:
        raise WebSearchError(f"Unsupported web search provider: {provider_type}")
    params = loads(parameters, {})
    if not isinstance(params, dict):
        params = {}
    _validate_parameters(provider_type, params)


def _tavily(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Tavily")
    status, body = _search_http(
        parameters,
        "POST",
        "https://api.tavily.com/search",
        headers={"Authorization": "Bearer " + key},
        body={"api_key": key, "query": query, "max_results": limit, "include_answer": False},
    )
    payload = _json_response(status, body, "tavily")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("content") or ""),
            "score": item.get("score"),
            "source": "tavily",
        }
        for item in (payload.get("results") or [])[:limit]
        if isinstance(item, dict)
    ]


def _brave(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Brave")
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": limit})
    status, body = _search_http(
        parameters, "GET", url, headers={"X-Subscription-Token": key, "Accept": "application/json"}
    )
    payload = _json_response(status, body, "brave")
    web = payload.get("web") if isinstance(payload.get("web"), dict) else {}
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("description") or ""),
            "published_date": str(item.get("age") or ""),
            "source": "brave",
        }
        for item in (web.get("results") or [])[:limit]
        if isinstance(item, dict)
    ]


def _bing(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Bing")
    url = "https://api.bing.microsoft.com/v7.0/search?" + urllib.parse.urlencode({"q": query, "count": limit})
    status, body = _search_http(parameters, "GET", url, headers={"Ocp-Apim-Subscription-Key": key})
    payload = _json_response(status, body, "bing")
    pages = payload.get("webPages") if isinstance(payload.get("webPages"), dict) else {}
    return [
        {
            "title": str(item.get("name") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("snippet") or ""),
            "source": "bing",
        }
        for item in (pages.get("value") or [])[:limit]
        if isinstance(item, dict)
    ]


def _google(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    engine = str(parameters.get("engine_id") or parameters.get("cx") or "")
    if not key or not engine:
        raise WebSearchError("API key and engine_id are required for Google")
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(
        {"key": key, "cx": engine, "q": query, "num": min(10, limit)}
    )
    status, body = _search_http(parameters, "GET", url)
    payload = _json_response(status, body, "google")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("link") or ""),
            "content": str(item.get("snippet") or ""),
            "source": "google",
        }
        for item in (payload.get("items") or [])[:limit]
        if isinstance(item, dict)
    ]


def _ollama(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    base = str(parameters.get("base_url") or "https://ollama.com").rstrip("/")
    if not key:
        raise WebSearchError("API key is required for Ollama")
    status, body = _search_http(
        parameters,
        "POST",
        base + "/api/web_search",
        headers={"Authorization": "Bearer " + key},
        body={"query": query, "max_results": limit},
    )
    payload = _json_response(status, body, "ollama")
    rows = payload.get("results") or payload.get("data") or []
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or item.get("link") or ""),
            "content": str(item.get("content") or item.get("snippet") or ""),
            "source": "ollama",
        }
        for item in rows[:limit]
        if isinstance(item, dict)
    ]


def _serply(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Serply")
    url = "https://api.serply.io/v1/search?" + urllib.parse.urlencode({"q": query, "num": limit})
    status, body = _search_http(
        parameters,
        "GET",
        url,
        headers={"X-Api-Key": key, "Accept": "application/json", "User-Agent": "WeKnora/1.0"},
    )
    payload = _json_response(status, body, "serply")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("link") or ""),
            "content": str(item.get("description") or ""),
            "source": "serply",
        }
        for item in (payload.get("results") or [])[:limit]
        if isinstance(item, dict)
    ]


def _baidu(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Baidu")
    status, body = _search_http(
        parameters,
        "POST",
        "https://qianfan.baidubce.com/v2/ai_search/web_search",
        headers={"Authorization": "Bearer " + key},
        body={
            "messages": [{"role": "user", "content": _truncate_width(query, 72)}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": limit}],
        },
    )
    payload = _json_response(status, body, "baidu")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise WebSearchError(f"baidu API error {code}: {payload.get('message') or ''}")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("content") or ""),
            "published_date": str(item.get("date") or ""),
            "source": "baidu",
        }
        for item in (payload.get("references") or [])[:limit]
        if isinstance(item, dict)
    ]


def _keenable(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    headers = {"X-Keenable-Title": "WeKnora"}
    if key:
        headers["X-API-Key"] = key
    status, body = _search_http(
        parameters,
        "POST",
        "https://api.keenable.ai/v1/search" if key else "https://api.keenable.ai/v1/search/public",
        headers=headers,
        body={"query": query, "mode": "pro"},
    )
    payload = _json_response(status, body, "keenable")
    results = []
    for item in (payload.get("results") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("description") or item.get("snippet") or "")
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": snippet,
                "published_date": str(item.get("published_at") or ""),
                "source": "keenable",
            }
        )
    return results


_METASO_SCOPES = {
    "webpage": "webpages",
    "document": "documents",
    "scholar": "scholars",
    "podcast": "podcasts",
    "video": "videos",
    "image": "images",
}


def _metaso(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Metaso")
    scope = str(_extra_config(parameters).get("scope") or "webpage").strip().lower()
    if scope not in _METASO_SCOPES:
        scope = "webpage"
    status, body = _search_http(
        parameters,
        "POST",
        "https://metaso.cn/api/v1/search",
        headers={"Authorization": "Bearer " + key},
        body={
            "q": query,
            "scope": scope,
            "size": limit,
            "includeSummary": True,
            "includeRawContent": False,
            "conciseSnippet": True,
        },
    )
    payload = _json_response(status, body, "metaso")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("link") or ""),
            "content": str(item.get("summary") or item.get("snippet") or ""),
            "published_date": str(item.get("date") or ""),
            "source": "metaso",
        }
        for item in (payload.get(_METASO_SCOPES[scope]) or [])[:limit]
        if isinstance(item, dict)
    ]


def _zhipu(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Zhipu")
    extra = _extra_config(parameters)
    engine = str(extra.get("search_engine") or "search_std")
    if engine not in {"search_std", "search_pro", "search_pro_sogou", "search_pro_quark"}:
        engine = "search_std"
    content_size = str(extra.get("content_size") or "medium")
    if content_size not in {"medium", "high"}:
        content_size = "medium"
    status, body = _search_http(
        parameters,
        "POST",
        "https://open.bigmodel.cn/api/paas/v4/web_search",
        headers={"Authorization": "Bearer " + key},
        body={
            "search_query": query[:70],
            "search_engine": engine,
            "search_intent": False,
            "count": limit,
            "content_size": content_size,
        },
    )
    payload = _json_response(status, body, "zhipu")
    error = payload.get("error")
    if isinstance(error, dict) and (error.get("code") or error.get("message")):
        raise WebSearchError(f"zhipu API error {error.get('code')}: {error.get('message') or ''}")
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("link") or ""),
            "content": str(item.get("content") or ""),
            "published_date": str(item.get("publish_date") or ""),
            "source": "zhipu",
        }
        for item in (payload.get("search_result") or [])[:limit]
        if isinstance(item, dict)
    ]


def _exa(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Exa")
    include_text = str(_extra_config(parameters).get("include_text") or "").strip().lower() == "true"
    status, body = _search_http(
        parameters,
        "POST",
        "https://api.exa.ai/search",
        headers={"x-api-key": key},
        body={
            "query": query,
            "numResults": limit,
            "contents": {"highlights": True, "text": include_text},
        },
    )
    payload = _json_response(status, body, "exa")
    results = []
    for item in (payload.get("results") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        highlights = item.get("highlights")
        if isinstance(highlights, list):
            snippet = "\n".join(str(value) for value in highlights)
        else:
            snippet = str(highlights or "")
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": text[:12000],
                "snippet": snippet or text[:500],
                "published_date": str(item.get("publishedDate") or ""),
                "source": "exa",
            }
        )
    return results


def _bocha(parameters: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key(parameters)
    if not key:
        raise WebSearchError("API key is required for Bocha")
    extra = _extra_config(parameters)
    freshness = str(extra.get("freshness") or "noLimit")
    if freshness not in {"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}:
        freshness = "noLimit"
    raw_summary = extra.get("summary", "true")
    summary = not (raw_summary is False or str(raw_summary).strip().lower() == "false")
    status, body = _search_http(
        parameters,
        "POST",
        "https://api.bochaai.com/v1/web-search",
        headers={"Authorization": "Bearer " + key},
        body={"query": query, "freshness": freshness, "summary": summary, "count": limit},
    )
    payload = _json_response(status, body, "bocha")
    code = payload.get("code")
    if code not in (0, 200, "0", "200"):
        raise WebSearchError(f"bocha API error {code}: {payload.get('msg') or payload.get('message') or ''}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    pages = data.get("webPages") if isinstance(data.get("webPages"), dict) else {}
    return [
        {
            "title": str(item.get("name") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("summary") or item.get("snippet") or ""),
            "published_date": str(item.get("datePublished") or item.get("dateLastCrawled") or ""),
            "source": "bocha",
        }
        for item in (pages.get("value") or [])[:limit]
        if isinstance(item, dict)
    ]


PROVIDERS = {
    "duckduckgo": _duckduckgo,
    "searxng": _searxng,
    "tavily": _tavily,
    "brave": _brave,
    "bing": _bing,
    "google": _google,
    "ollama": _ollama,
    "serply": _serply,
    "baidu": _baidu,
    "keenable": _keenable,
    "metaso": _metaso,
    "zhipu": _zhipu,
    "exa": _exa,
    "bocha": _bocha,
}


def search(provider: str, parameters: Any, query: str, limit: int = 5) -> list[dict[str, Any]]:
    provider_type = str(provider or "duckduckgo").strip().lower()
    if provider_type not in PROVIDERS:
        raise WebSearchError(f"Unsupported web search provider: {provider_type}")
    params = loads(parameters, {})
    if not isinstance(params, dict):
        params = {}
    _validate_parameters(provider_type, params)
    query = str(query or "").strip()
    if not query:
        return []
    return PROVIDERS[provider_type](params, query, min(20, max(1, int(limit))))


def default_provider(tenant_id: int) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT * FROM web_search_providers WHERE tenant_id=? AND deleted_at IS NULL ORDER BY is_default DESC, created_at LIMIT 1",
        (tenant_id,),
    )
