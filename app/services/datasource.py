"""Connector helpers used by the datasource API.

The Go project ships a connector per provider. This module implements the
self-contained connectors that need no vendor SDK: feed, web-page and DingTalk
knowledge-base sources. Other connector types remain discoverable but report a
clear validation error until their provider-specific client is configured.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from typing import Any
from xml.etree import ElementTree

from . import dingtalk, seafile, url_guard
from ..utils import loads


CONNECTORS: tuple[dict[str, Any], ...] = (
    {
        "type": "feishu", "name": "Feishu (飞书)", "description": "Sync documents, wikis, and content from Feishu",
        "icon": "", "priority": 0, "auth_type": "oauth2", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "lark", "name": "Lark", "description": "Sync documents, wikis, and content from Lark",
        "icon": "", "priority": 0, "auth_type": "oauth2", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "feishu_drive", "name": "Feishu Drive (飞书云盘)", "description": "Sync files from a Feishu Drive folder",
        "icon": "", "priority": 0, "auth_type": "oauth2", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "lark_drive", "name": "Lark Drive", "description": "Sync files from a Lark Drive folder",
        "icon": "", "priority": 0, "auth_type": "oauth2", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "notion", "name": "Notion", "description": "Sync pages and databases from Notion",
        "icon": "", "priority": 1, "auth_type": "api_key", "capabilities": ["incremental"],
    },
    {
        "type": "confluence", "name": "Confluence", "description": "Sync spaces and pages from Atlassian Confluence",
        "icon": "", "priority": 2, "auth_type": "api_key", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "yuque", "name": "Yuque (语雀)", "description": "Sync knowledge bases and documents from Yuque",
        "icon": "", "priority": 3, "auth_type": "api_key", "capabilities": ["incremental"],
    },
    {
        "type": "ima", "name": "Tencent IMA (ima.qq.com)", "description": "Sync knowledge bases from Tencent IMA",
        "icon": "", "priority": 3, "auth_type": "api_key", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "github", "name": "GitHub", "description": "Sync repositories, wikis, and issues from GitHub",
        "icon": "", "priority": 4, "auth_type": "oauth2", "capabilities": ["incremental"],
    },
    {
        "type": "google_drive", "name": "Google Drive", "description": "Sync documents and files from Google Drive",
        "icon": "", "priority": 5, "auth_type": "oauth2", "capabilities": ["incremental"],
    },
    {
        "type": "onedrive", "name": "OneDrive / SharePoint", "description": "Sync documents and files from Microsoft OneDrive",
        "icon": "", "priority": 6, "auth_type": "oauth2", "capabilities": ["incremental"],
    },
    {
        "type": "dingtalk", "name": "DingTalk (钉钉)", "description": "Sync online documents from DingTalk knowledge bases",
        "icon": "", "priority": 7, "auth_type": "oauth2", "capabilities": ["incremental", "deletion_sync"],
    },
    {
        "type": "gitlab", "name": "GitLab", "description": "Sync files from GitLab projects",
        "icon": "", "priority": 8, "auth_type": "token", "capabilities": ["incremental", "hierarchical"],
    },
    {
        "type": "web_crawler", "name": "Web Crawler (Sitemap)", "description": "Crawl websites via a URL or Sitemap.xml",
        "icon": "", "priority": 9, "auth_type": "none", "capabilities": [],
    },
    {
        "type": "slack", "name": "Slack", "description": "Sync channel messages and files from Slack",
        "icon": "", "priority": 10, "auth_type": "oauth2", "capabilities": ["incremental"],
    },
    {
        "type": "imap", "name": "Email (IMAP)", "description": "Sync email content from IMAP servers",
        "icon": "", "priority": 11, "auth_type": "password", "capabilities": [],
    },
    {
        "type": "rss", "name": "RSS / Atom Feed", "description": "Sync articles from RSS/Atom feeds",
        "icon": "", "priority": 12, "auth_type": "custom", "capabilities": ["incremental"],
    },
    {
        "type": "seafile", "name": "Seafile", "description": "Sync folders and files from Seafile libraries",
        "icon": "", "priority": 13, "auth_type": "token", "capabilities": ["incremental", "hierarchical", "deletion_sync"],
    },
)

_SUPPORTED_SYNC_TYPES = {"rss", "web_crawler", "web", "dingtalk", "seafile"}


def connector_types() -> list[dict[str, Any]]:
    return [dict(item) for item in CONNECTORS]


def _connector_kind(source_type: str) -> str:
    return "web_crawler" if source_type == "web" else source_type


def prepare_config(raw: Any) -> dict[str, Any]:
    """Return a flat connector config with stored credentials merged in."""
    value = loads(raw, {})
    if not isinstance(value, dict):
        value = {}
    credentials = value.get("_credentials")
    if not isinstance(credentials, dict):
        credentials = value.get("credentials") if isinstance(value.get("credentials"), dict) else {}
    settings = value.get("settings") if isinstance(value.get("settings"), dict) else {}
    merged = {**value, **credentials}
    merged["settings"] = settings
    merged.pop("_credentials", None)
    merged.pop("credentials", None)
    return merged


def _split_values(*values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, list):
            parts = value
        else:
            parts = re.split(r"[\r\n,]+", str(value or ""))
        for part in parts:
            item = str(part).strip()
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _resource_ids(config: dict[str, Any]) -> list[str]:
    return _split_values(config.get("resource_ids") or [])


def _headers(config: dict[str, Any]) -> dict[str, str]:
    raw = config.get("auth_headers") or (config.get("settings") or {}).get("auth_headers") or ""
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("key"):
                result[str(item["key"])] = str(item.get("value") or "")
        return result
    result = {}
    for line in str(raw).splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip():
            result[name.strip()] = value.strip()
    return result


def _feed_urls(config: dict[str, Any]) -> list[str]:
    settings = config.get("settings") or {}
    return _split_values(
        settings.get("feed_urls"),
        config.get("feed_urls"),
        _resource_ids(config),
    )


def _web_urls(config: dict[str, Any]) -> list[str]:
    settings = config.get("settings") or {}
    return _split_values(
        settings.get("urls"),
        settings.get("start_url"),
        config.get("urls"),
        config.get("start_url"),
        _resource_ids(config),
    )


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> bytes:
    data, _, _ = url_guard.fetch_public_url(
        url,
        headers={"User-Agent": "PyWeKnora/0.8 (+https://github.com/Tencent/WeKnora)", **(headers or {})},
        timeout=timeout,
        max_bytes=50 * 1024 * 1024,
    )
    return data


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _child_text(node: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def _child(node: ElementTree.Element, *names: str) -> ElementTree.Element | None:
    wanted = {name.lower() for name in names}
    return next((child for child in node if _local_name(child.tag) in wanted), None)


def _html_text(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", value)).strip()


def _fingerprint(title: str, url: str, content: str) -> str:
    payload = json.dumps([title, url, content], ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def _parse_feed(data: bytes, feed_url: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(data)
    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        title = _child_text(entry, "title")
        link = _child_text(entry, "link")
        link_node = _child(entry, "link")
        if not link and link_node is not None:
            link = link_node.attrib.get("href", "")
        external_id = _child_text(entry, "guid", "id") or link or title
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        content = _child_text(entry, "encoded", "content", "description", "summary")
        content = _html_text(content)
        published = _child_text(entry, "updated", "published", "pubDate", "date")
        item_url = link or feed_url
        title = html.unescape(title).strip() or content[:80] or item_url
        result.append({
            "external_id": external_id,
            "title": title,
            "content": content or title,
            "url": item_url,
            "updated_at": published,
            "resource_id": feed_url,
            "fingerprint": _fingerprint(title, item_url, content or title),
        })
    return result


def _page_title(text: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""


def _parse_page(data: bytes, url: str) -> dict[str, Any]:
    text = data.decode("utf-8", errors="replace")
    title = _page_title(text) or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or url
    body = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", text, flags=re.I | re.S)
    body = re.sub(r"</(p|div|li|h[1-6]|tr|section|article)>", "\n", body, flags=re.I)
    content = _html_text(body)
    return {
        "external_id": url, "title": title, "content": content or title, "url": url,
        "updated_at": "", "resource_id": url,
        "fingerprint": _fingerprint(title, url, content or title),
    }


def list_resources(source_type: str, config: dict[str, Any], parent_id: str = "") -> list[dict[str, Any]]:
    source_type = _connector_kind(source_type)
    if source_type == "dingtalk":
        return dingtalk.list_resources(config, parent_id)
    if source_type == "seafile":
        return seafile.list_resources(config, parent_id)
    if source_type == "rss":
        if parent_id:
            return []
        resources = []
        headers = _headers(config)
        for url in _feed_urls(config):
            item = {"external_id": url, "name": url, "type": "feed", "description": "", "url": url}
            try:
                data = _http_get(url, headers)
                parsed = _parse_feed(data, url)
                title = next((node for node in ElementTree.fromstring(data).iter() if _local_name(node.tag) == "title"), None)
                if title is not None and "".join(title.itertext()).strip():
                    item["name"] = "".join(title.itertext()).strip()
                item["metadata"] = {"item_count": len(parsed)}
            except Exception as exc:  # noqa: BLE001 - surfaced in the resource row
                item["description"] = f"fetch failed: {exc}"
            resources.append(item)
        return resources
    if source_type == "web_crawler":
        if parent_id:
            return []
        return [
            {"external_id": url, "name": url, "type": "page", "description": "", "url": url}
            for url in _web_urls(config)
        ]
    raise ValueError(f"connector {source_type!r} is not available in the Python build")


def validate(source_type: str, config: dict[str, Any]) -> dict[str, Any]:
    source_type = _connector_kind(source_type)
    if source_type == "dingtalk":
        return dingtalk.validate(config)
    if source_type == "seafile":
        return seafile.validate(config)
    if source_type == "rss":
        urls = _feed_urls(config)
        if not urls:
            raise ValueError("feed_urls is required")
        headers = _headers(config)
        count = 0
        for url in urls:
            count += len(_parse_feed(_http_get(url, headers), url))
        return {"valid": True, "items": count, "resources": len(urls)}
    if source_type == "web_crawler":
        urls = _web_urls(config)
        if not urls:
            raise ValueError("url is required")
        for url in urls:
            _http_get(url)
        return {"valid": True, "items": len(urls), "resources": len(urls)}
    raise ValueError(f"connector {source_type!r} is not available in the Python build")


def fetch_items(
    source_type: str,
    config: dict[str, Any],
    previous_cursor: dict[str, Any] | None = None,
    *,
    force_full: bool = False,
    cursor_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    source_type = _connector_kind(source_type)
    if source_type == "dingtalk":
        return dingtalk.fetch_items(config, previous_cursor)
    if source_type == "seafile":
        return seafile.fetch_items(
            config,
            previous_cursor,
            force_full=force_full,
            cursor_meta=cursor_meta,
        )
    if source_type == "rss":
        selected = set(_resource_ids(config))
        items: list[dict[str, Any]] = []
        headers = _headers(config)
        for url in _feed_urls(config):
            if selected and url not in selected:
                continue
            items.extend(_parse_feed(_http_get(url, headers), url))
        return items
    if source_type == "web_crawler":
        selected = set(_resource_ids(config))
        items = []
        for url in _web_urls(config):
            if selected and url not in selected:
                continue
            items.append(_parse_page(_http_get(url), url))
        return items
    raise ValueError(f"connector {source_type!r} is not available in the Python build")


def supports_sync(source_type: str) -> bool:
    return _connector_kind(source_type) in _SUPPORTED_SYNC_TYPES


def resource_ancestors(source_type: str, config: dict[str, Any], ids: list[str]) -> list[str]:
    del config
    if _connector_kind(source_type) == "seafile":
        return seafile.resource_ancestors(ids)
    return []
