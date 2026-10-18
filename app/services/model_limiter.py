"""Concurrency governor for background model calls.

The governor is process-local unless ``REDIS_ADDR`` is configured. In Redis
mode, each held slot is a sorted-set member leased by expiry time so that a
crashed process cannot permanently consume a model's concurrency budget.
Backend failures fail open and never block model traffic.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator
from urllib.parse import urlsplit

from .. import db


DEFAULT_LIMIT = 32
REDIS_LEASE_SECONDS = 30.0
REDIS_POLL_SECONDS = 0.2
REDIS_KEY_PREFIX = "weknora:modelsem:"
REDIS_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], ARGV[1] + ARGV[4], ARGV[3])
    redis.call('PEXPIRE', KEYS[1], ARGV[4] * 2)
    return 1
end
return 0
"""

logger = logging.getLogger(__name__)
_background: ContextVar[bool] = ContextVar("pyweknora_model_background", default=False)
_lock = threading.Lock()
_backend_lock = threading.Lock()
_semaphores: dict[str, "_ModelSemaphore"] = {}
_redis_backend: "_RedisModelLimiter | None" = None
_redis_backend_settings: tuple[Any, ...] | None = None


def _noop() -> None:
    return None


class _ModelSemaphore:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active = 0
        self.waiting = 0
        self.limit = 0
        self.name = ""

    def acquire(self, limit: int) -> Any:
        with self.condition:
            self.limit = limit
            self.waiting += 1
            try:
                while self.active >= limit:
                    self.condition.wait()
                self.active += 1
            finally:
                self.waiting -= 1

        released = False

        def release() -> None:
            nonlocal released
            with self.condition:
                if released:
                    return
                released = True
                self.active -= 1
                self.condition.notify()

        return release


class _TrackedModel:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.name = ""
        self.limit = 0
        self.waiting = 0


class _RedisModelLimiter:
    def __init__(
        self,
        client: Any,
        *,
        lease_seconds: float = REDIS_LEASE_SECONDS,
        poll_seconds: float = REDIS_POLL_SECONDS,
    ) -> None:
        self.client = client
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.acquire_script = client.register_script(REDIS_ACQUIRE_SCRIPT)
        self.tracked: dict[str, _TrackedModel] = {}
        self.tracked_lock = threading.Lock()

    def _track(self, model_id: str, name: str, limit: int, waiting_delta: int = 0) -> None:
        with self.tracked_lock:
            tracked = self.tracked.get(model_id)
            if tracked is None:
                tracked = _TrackedModel()
                self.tracked[model_id] = tracked
        with tracked.lock:
            tracked.limit = limit
            tracked.waiting += waiting_delta
            if name:
                tracked.name = name

    def acquire(self, model_id: str, limit: int, name: str) -> Any:
        if limit <= 0 or not model_id:
            return _noop
        key = REDIS_KEY_PREFIX + model_id
        token = str(uuid.uuid4())
        ttl_ms = int(self.lease_seconds * 1000)
        self._track(model_id, name, limit, 1)
        try:
            while True:
                now_ms = int(time.time() * 1000)
                try:
                    admitted = int(
                        self.acquire_script(
                            keys=[key],
                            args=[now_ms, limit, token, ttl_ms],
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - limiter outage fails open
                    logger.warning("model limiter acquire failed for %s: %s", model_id, exc)
                    return _noop
                if admitted == 1:
                    return self._hold(key, token)
                time.sleep(self.poll_seconds)
        finally:
            self._track(model_id, name, limit, -1)

    def _hold(self, key: str, token: str) -> Any:
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(self.lease_seconds / 3):
                now_ms = int(time.time() * 1000)
                try:
                    self.client.zadd(key, {token: now_ms + int(self.lease_seconds * 1000)})
                    self.client.pexpire(key, int(self.lease_seconds * 2000))
                except Exception as exc:  # noqa: BLE001 - expiry remains the fallback
                    logger.warning("model limiter heartbeat failed for %s: %s", key, exc)

        threading.Thread(target=heartbeat, daemon=True).start()
        released = False
        release_lock = threading.Lock()

        def release() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            stop.set()
            try:
                self.client.zrem(key, token)
            except Exception as exc:  # noqa: BLE001 - the lease will expire
                logger.warning("model limiter release failed for %s: %s", key, exc)

        return release

    def runtime_stats(self) -> list[dict[str, Any]]:
        with self.tracked_lock:
            tracked_items = list(self.tracked.items())
        now_ms = int(time.time() * 1000)
        stats = []
        for model_id, tracked in sorted(tracked_items, key=lambda item: item[0]):
            try:
                active = int(self.client.zcount(REDIS_KEY_PREFIX + model_id, now_ms + 1, "+inf"))
            except Exception as exc:  # noqa: BLE001 - stats must not break the endpoint
                logger.warning("model limiter stats failed for %s: %s", model_id, exc)
                continue
            with tracked.lock:
                stats.append(
                    {
                        "model_id": model_id,
                        "name": tracked.name,
                        "active": active,
                        "waiting": tracked.waiting,
                        "limit": tracked.limit,
                    }
                )
        return stats


def _configured_limit() -> int:
    raw_setting: Any = None
    try:
        row = db.fetch_one("SELECT value FROM system_settings WHERE key='model.max_concurrency'")
        raw_setting = row.get("value") if row else None
    except Exception:  # noqa: BLE001 - limiter availability must not break model traffic
        raw_setting = None
    raw = raw_setting if raw_setting is not None else os.getenv("WEKNORA_MODEL_MAX_CONCURRENCY", "")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _model_semaphore(model_id: str) -> _ModelSemaphore:
    with _lock:
        semaphore = _semaphores.get(model_id)
        if semaphore is None:
            semaphore = _ModelSemaphore()
            _semaphores[model_id] = semaphore
        return semaphore


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def _redis_settings() -> tuple[Any, ...] | None:
    address = os.getenv("REDIS_ADDR", "").strip()
    if not address:
        return None
    try:
        database = int(os.getenv("REDIS_DB", "").strip() or 0)
    except ValueError:
        database = 0
    return (
        address,
        database,
        os.getenv("REDIS_USERNAME", "").strip(),
        os.getenv("REDIS_PASSWORD", ""),
        _env_true("REDIS_USE_TLS"),
        os.getenv("REDIS_TLS_SERVER_NAME", "").strip(),
        _env_true("REDIS_TLS_INSECURE_SKIP_VERIFY"),
    )


def _build_redis_client(settings: tuple[Any, ...]) -> Any:
    try:
        import redis
    except ImportError:
        logger.warning("REDIS_ADDR is configured but the redis package is not installed")
        return None
    address, database, username, password, use_tls, server_name, insecure = settings
    parsed = urlsplit(address if "://" in address else f"//{address}")
    try:
        port = parsed.port or 6379
    except ValueError:
        port = 6379
    use_tls = use_tls or parsed.scheme == "rediss"
    connection = {
        "host": parsed.hostname or "localhost",
        "port": port,
        "db": database,
        "username": username or None,
        "password": password or None,
        "decode_responses": True,
    }
    if not use_tls:
        return redis.Redis(**connection)

    class _ServerNameSSLConnection(redis.connection.SSLConnection):
        def __init__(self, *args: Any, server_name: str = "", **kwargs: Any) -> None:
            self.server_name = server_name
            super().__init__(*args, **kwargs)

        def _wrap_socket_with_ssl(self, sock: Any) -> Any:
            host = self.host
            if self.server_name:
                self.host = self.server_name
            try:
                return super()._wrap_socket_with_ssl(sock)
            finally:
                self.host = host

    pool = redis.connection.ConnectionPool(
        connection_class=_ServerNameSSLConnection,
        server_name=server_name,
        ssl_cert_reqs="none" if insecure else "required",
        ssl_check_hostname=not insecure,
        **connection,
    )
    return redis.Redis(connection_pool=pool)


def _redis_limiter() -> _RedisModelLimiter | None:
    global _redis_backend, _redis_backend_settings
    settings = _redis_settings()
    if settings is None:
        return None
    with _backend_lock:
        if settings != _redis_backend_settings:
            if _redis_backend is not None:
                try:
                    _redis_backend.client.close()
                except Exception:  # noqa: BLE001 - replacing a best-effort client
                    pass
            client = _build_redis_client(settings)
            _redis_backend = _RedisModelLimiter(client) if client is not None else None
            _redis_backend_settings = settings
        return _redis_backend


@contextmanager
def background() -> Iterator[None]:
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def acquire(runtime: dict[str, Any]) -> Any:
    if not _background.get():
        return _noop
    model_id = str(runtime.get("id") or runtime.get("name") or "").strip()
    if not model_id:
        return _noop
    try:
        model_limit = int(runtime.get("max_concurrency") or 0)
    except (TypeError, ValueError):
        model_limit = 0
    limit = model_limit if model_limit > 0 else _configured_limit()
    if limit <= 0:
        return _noop
    name = str(runtime.get("display_name") or runtime.get("name") or model_id)
    redis_limiter = _redis_limiter()
    if redis_limiter is not None:
        return redis_limiter.acquire(model_id, limit, name)
    semaphore = _model_semaphore(model_id)
    if not semaphore.name:
        semaphore.name = name
    return semaphore.acquire(limit)


def runtime_stats() -> list[dict[str, Any]]:
    redis_limiter = _redis_limiter()
    if redis_limiter is not None:
        return redis_limiter.runtime_stats()
    with _lock:
        semaphores = list(_semaphores.items())
    stats = []
    for model_id, semaphore in sorted(semaphores, key=lambda item: item[0]):
        with semaphore.condition:
            stats.append(
                {
                    "model_id": model_id,
                    "name": semaphore.name,
                    "active": semaphore.active,
                    "waiting": semaphore.waiting,
                    "limit": semaphore.limit,
                }
            )
    return stats
