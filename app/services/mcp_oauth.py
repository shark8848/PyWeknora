"""MCP OAuth 2.1 authorization-code flow with discovery and PKCE."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..utils import loads


STATE_TTL_SECONDS = 10 * 60
REFRESH_SKEW_SECONDS = 30


class OAuthError(RuntimeError):
    pass


def _mapping(value: Any) -> dict[str, Any]:
    result = loads(value, {})
    return result if isinstance(result, dict) else {}


def _now_epoch() -> float:
    return time.time()


def _prune() -> None:
    now = _now_epoch()
    with db.transaction() as conn:
        conn.execute("DELETE FROM py_mcp_oauth_states WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM py_mcp_oauth_attempts WHERE expires_at<=?", (now,))


def _safe_url(raw: str, label: str) -> str:
    parsed = urllib.parse.urlparse(str(raw or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OAuthError(f"{label} must be an absolute http(s) URL")
    if (parsed.hostname or "").lower() in {"169.254.169.254", "metadata.google.internal"}:
        raise OAuthError(f"{label} points at a blocked metadata host")
    return urllib.parse.urlunparse(parsed)


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    body: bytes | None = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise OAuthError(f"OAuth endpoint returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"OAuth endpoint connection failed: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise OAuthError("OAuth endpoint returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise OAuthError("OAuth endpoint returned a non-object JSON response")
    return result


def validate_frontend_redirect(raw: str) -> str:
    value = str(raw or "").strip() or "/"
    if any(character in value for character in ("\\", "\r", "\n", "\t", "#")):
        raise OAuthError("invalid frontend_redirect")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        trusted = urllib.parse.urlparse(
            os.getenv("APP_EXTERNAL_URL") or os.getenv("WEKNORA_APP_EXTERNAL_URL") or ""
        )
        if (
            parsed.scheme not in {"http", "https"}
            or not trusted.netloc
            or parsed.scheme.lower() != trusted.scheme.lower()
            or parsed.netloc.lower() != trusted.netloc.lower()
        ):
            raise OAuthError("frontend_redirect origin is not configured")
        return urllib.parse.urlunparse(parsed)
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise OAuthError("frontend_redirect must be an application-relative path")
    return value


def _protected_resource_candidates(service_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(service_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = []
    if path:
        candidates.append(origin + "/.well-known/oauth-protected-resource" + path)
    candidates.append(origin + "/.well-known/oauth-protected-resource")
    return list(dict.fromkeys(candidates))


def _authorization_server_candidates(issuer: str) -> list[str]:
    parsed = urllib.parse.urlparse(issuer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = []
    for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        if path:
            candidates.append(origin + suffix + path)
        candidates.append(origin + suffix)
    return list(dict.fromkeys(candidates))


def _load_json_candidate(urls: list[str]) -> dict[str, Any] | None:
    for url in urls:
        try:
            return _http_json("GET", _safe_url(url, "OAuth metadata URL"))
        except OAuthError:
            continue
    return None


def discover_authorization_server(service: dict[str, Any]) -> dict[str, Any]:
    auth_config = _mapping(service.get("auth_config"))
    service_url = _safe_url(str(service.get("url") or ""), "MCP service URL")
    pinned = str(auth_config.get("auth_server_metadata_url") or "").strip()
    if pinned:
        metadata = _http_json("GET", _safe_url(pinned, "OAuth metadata URL"))
    else:
        protected = _load_json_candidate(_protected_resource_candidates(service_url))
        issuers: list[str] = []
        if protected:
            for issuer in protected.get("authorization_servers") or []:
                if isinstance(issuer, str) and issuer.strip():
                    issuers.append(issuer.strip())
        if not issuers:
            parsed = urllib.parse.urlparse(service_url)
            issuers.append(f"{parsed.scheme}://{parsed.netloc}")
        metadata = None
        for issuer in issuers:
            metadata = _load_json_candidate(_authorization_server_candidates(issuer))
            if metadata:
                break
        if not metadata:
            raise OAuthError("OAuth authorization server metadata was not found")

    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if metadata.get(key):
            metadata[key] = _safe_url(str(metadata[key]), key)
    if not metadata.get("authorization_endpoint") or not metadata.get("token_endpoint"):
        raise OAuthError("OAuth metadata is missing authorization_endpoint or token_endpoint")
    return metadata


def _client_row(tenant_id: int, service_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT * FROM mcp_oauth_clients WHERE tenant_id=? AND service_id=?",
        (tenant_id, service_id),
    )


def _save_client(
    tenant_id: int,
    service_id: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> None:
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_oauth_clients(id, tenant_id, service_id, client_id, client_secret, redirect_uri, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, service_id) DO UPDATE SET client_id=excluded.client_id, "
            "client_secret=excluded.client_secret, redirect_uri=excluded.redirect_uri, updated_at=excluded.updated_at",
            (db.new_id(), tenant_id, service_id, client_id, client_secret, redirect_uri, now, now),
        )


def _register_client(
    service: dict[str, Any], metadata: dict[str, Any], redirect_uri: str
) -> tuple[str, str, str]:
    tenant_id = int(service.get("tenant_id") or 0)
    row = _client_row(tenant_id, str(service.get("id") or ""))
    if row:
        return (
            str(row.get("client_id") or ""),
            str(row.get("client_secret") or ""),
            str(row.get("redirect_uri") or redirect_uri),
        )
    endpoint = str(metadata.get("registration_endpoint") or "")
    if not endpoint:
        raise OAuthError("OAuth server does not support dynamic client registration")
    auth_config = _mapping(service.get("auth_config"))
    registration = _http_json(
        "POST",
        endpoint,
        payload={
            "client_name": "WeKnora",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(str(item) for item in auth_config.get("scopes") or []),
        },
    )
    client_id = str(registration.get("client_id") or "").strip()
    if not client_id:
        raise OAuthError("dynamic client registration returned an empty client_id")
    client_secret = str(registration.get("client_secret") or "")
    _save_client(
        tenant_id,
        str(service.get("id") or ""),
        client_id,
        client_secret,
        redirect_uri,
    )
    return client_id, client_secret, redirect_uri


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_authorization(
    service: dict[str, Any],
    tenant_id: int,
    user_id: str,
    redirect_uri: str,
    frontend_redirect: str,
) -> tuple[str, str]:
    if str(_mapping(service.get("auth_config")).get("auth_type") or "").lower() != "oauth":
        raise OAuthError("MCP service is not configured to use OAuth")
    redirect_uri = _safe_url(redirect_uri, "redirect_uri")
    frontend_redirect = validate_frontend_redirect(frontend_redirect)
    metadata = discover_authorization_server(service)
    client_id, client_secret, registered_redirect = _register_client(service, metadata, redirect_uri)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    auth_config = _mapping(service.get("auth_config"))
    scopes = [str(item) for item in auth_config.get("scopes") or [] if str(item).strip()]
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": registered_redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        query["scope"] = " ".join(scopes)
    resource = str(service.get("url") or "")
    if resource:
        query["resource"] = resource
    separator = "&" if "?" in str(metadata["authorization_endpoint"]) else "?"
    authorization_url = str(metadata["authorization_endpoint"]) + separator + urllib.parse.urlencode(query)
    now = _now_epoch()
    attempt = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "service_id": str(service.get("id") or ""),
        "completed": False,
        "expires_at": now + STATE_TTL_SECONDS,
    }
    state_data = {
        **attempt,
        "code_verifier": verifier,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": registered_redirect,
        "frontend_redirect": frontend_redirect,
        "token_endpoint": str(metadata["token_endpoint"]),
        "auth_methods": metadata.get("token_endpoint_auth_methods_supported") or [],
        "resource": resource,
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_mcp_oauth_states(state, data, expires_at) VALUES (?, ?, ?)",
            (state, json.dumps(state_data, ensure_ascii=False), now + STATE_TTL_SECONDS),
        )
        conn.execute(
            "INSERT INTO py_mcp_oauth_attempts(id, tenant_id, user_id, service_id, completed, expires_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (state, tenant_id, user_id, str(service.get("id") or ""), now + STATE_TTL_SECONDS),
        )
    return authorization_url, state


def _token_auth_headers(client_id: str, client_secret: str, methods: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
    supported = {str(item) for item in methods or []}
    if not client_secret:
        return {}, {"client_id": client_id}
    if "client_secret_post" in supported and "client_secret_basic" not in supported:
        return {}, {"client_id": client_id, "client_secret": client_secret}
    raw = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return {"Authorization": "Basic " + raw}, {"client_id": client_id}


def _expires_at(seconds: Any) -> str:
    try:
        duration = int(seconds or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return ""
    return datetime.fromtimestamp(time.time() + duration, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _save_token(
    tenant_id: int,
    user_id: str,
    service_id: str,
    token: dict[str, Any],
    previous_refresh_token: str = "",
) -> None:
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise OAuthError("OAuth token response did not include access_token")
    refresh_token = str(token.get("refresh_token") or previous_refresh_token)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_oauth_tokens(id, tenant_id, user_id, principal_type, principal_id, service_id, "
            "access_token, refresh_token, token_type, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'web_user', ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, principal_type, principal_id, service_id) DO UPDATE SET "
            "user_id=excluded.user_id, access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
            "token_type=excluded.token_type, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (
                db.new_id(), tenant_id, user_id, user_id, service_id, access_token,
                refresh_token, str(token.get("token_type") or "Bearer"),
                _expires_at(token.get("expires_in")), now, now,
            ),
        )


def complete_authorization(state: str, code: str) -> dict[str, Any]:
    _prune()
    row = db.fetch_one(
        "SELECT * FROM py_mcp_oauth_states WHERE state=? AND expires_at>?",
        (state, _now_epoch()),
    )
    if not row:
        raise OAuthError("OAuth state is missing or expired")
    state_data = _mapping(row.get("data"))
    with db.transaction() as conn:
        conn.execute("DELETE FROM py_mcp_oauth_states WHERE state=?", (state,))
    frontend_redirect = str(state_data.get("frontend_redirect") or "/")
    client_secret = str(state_data.get("client_secret") or "")
    headers, form = _token_auth_headers(
        str(state_data.get("client_id") or ""),
        client_secret,
        list(state_data.get("auth_methods") or []),
    )
    form.update(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(state_data.get("redirect_uri") or ""),
            "code_verifier": str(state_data.get("code_verifier") or ""),
        }
    )
    if state_data.get("resource"):
        form["resource"] = str(state_data["resource"])
    token = _http_json("POST", str(state_data["token_endpoint"]), headers=headers, form=form)
    _save_token(
        int(state_data["tenant_id"]),
        str(state_data["user_id"]),
        str(state_data["service_id"]),
        token,
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE py_mcp_oauth_attempts SET completed=1, expires_at=? WHERE id=?",
            (_now_epoch() + STATE_TTL_SECONDS, state),
        )
    return {
        "frontend_redirect": frontend_redirect,
        "tenant_id": int(state_data["tenant_id"]),
        "user_id": str(state_data["user_id"]),
        "service_id": str(state_data["service_id"]),
    }


def attempt_complete(tenant_id: int, user_id: str, service_id: str, attempt_id: str) -> bool:
    _prune()
    attempt = db.fetch_one(
        "SELECT * FROM py_mcp_oauth_attempts WHERE id=? AND expires_at>?",
        (attempt_id, _now_epoch()),
    )
    if not attempt:
        return False
    return bool(
        int(attempt.get("completed") or 0)
        and int(attempt.get("tenant_id") or 0) == tenant_id
        and str(attempt.get("user_id") or "") == user_id
        and str(attempt.get("service_id") or "") == service_id
    )


def _parse_expiry(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def token_status(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row or not row.get("access_token"):
        return {"authorized": False, "state": "reauth_required", "refresh_available": False}
    refresh_available = bool(row.get("refresh_token"))
    expires = _parse_expiry(row.get("expires_at"))
    result: dict[str, Any] = {
        "authorized": True,
        "state": "authorized",
        "refresh_available": refresh_available,
    }
    if expires is not None:
        result["expires_at"] = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
        if expires.timestamp() <= time.time():
            result["authorized"] = False
            result["state"] = "refreshable" if refresh_available else "reauth_required"
    return result


def get_token(tenant_id: int, user_id: str, service_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT * FROM mcp_oauth_tokens WHERE tenant_id=? AND principal_type='web_user' "
        "AND principal_id=? AND service_id=?",
        (tenant_id, user_id, service_id),
    )


def is_authorized(tenant_id: int, user_id: str, service_id: str) -> bool:
    return bool(token_status(get_token(tenant_id, user_id, service_id)).get("authorized"))


def refresh_access_token(service: dict[str, Any], tenant_id: int, user_id: str) -> str:
    row = get_token(tenant_id, user_id, str(service.get("id") or ""))
    status = token_status(row)
    if status["authorized"]:
        return str((row or {}).get("access_token") or "")
    if not row or not row.get("refresh_token"):
        return ""
    expires = _parse_expiry(row.get("expires_at"))
    if expires is not None and expires.timestamp() > time.time() + REFRESH_SKEW_SECONDS:
        return str(row.get("access_token") or "")
    metadata = discover_authorization_server(service)
    client = _client_row(tenant_id, str(service.get("id") or ""))
    if not client:
        return ""
    headers, form = _token_auth_headers(
        str(client.get("client_id") or ""),
        str(client.get("client_secret") or ""),
        list(metadata.get("token_endpoint_auth_methods_supported") or []),
    )
    form.update({"grant_type": "refresh_token", "refresh_token": str(row["refresh_token"])})
    token = _http_json("POST", str(metadata["token_endpoint"]), headers=headers, form=form)
    _save_token(tenant_id, user_id, str(service.get("id") or ""), token, str(row["refresh_token"]))
    return str(token.get("access_token") or "")
