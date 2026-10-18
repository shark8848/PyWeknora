"""Opt-in integration coverage for the Redis model concurrency limiter."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("WEKNORA_REDIS_INTEGRATION") != "1",
    reason="set WEKNORA_REDIS_INTEGRATION=1 to exercise a real Redis server",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def redis_client():
    redis = pytest.importorskip("redis")
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server is not installed")
    port = _free_port()
    process = subprocess.Popen(
        [
            binary,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--protected-mode",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                if client.ping():
                    break
            except redis.RedisError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        yield client
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_redis_limiter_is_cluster_wide_and_reclaims_leases(redis_client) -> None:
    from app.services import model_limiter

    model_id = f"redis-limiter-{uuid.uuid4()}"
    key = model_limiter.REDIS_KEY_PREFIX + model_id
    limiter_a = model_limiter._RedisModelLimiter(
        redis_client, lease_seconds=0.3, poll_seconds=0.01
    )
    limiter_b = model_limiter._RedisModelLimiter(
        redis_client, lease_seconds=0.3, poll_seconds=0.01
    )
    release_a = limiter_a.acquire(model_id, 1, "Shared model")
    entered = threading.Event()
    release_b: list = []

    def wait_for_slot() -> None:
        release_b.append(limiter_b.acquire(model_id, 1, "Shared model"))
        entered.set()

    worker = threading.Thread(target=wait_for_slot)
    try:
        worker.start()
        deadline = time.monotonic() + 2
        waiting_stat = None
        while time.monotonic() < deadline:
            stats = limiter_b.runtime_stats()
            waiting_stat = next((item for item in stats if item["model_id"] == model_id), None)
            if waiting_stat and waiting_stat["waiting"] == 1:
                break
            time.sleep(0.01)
        assert waiting_stat == {
            "model_id": model_id,
            "name": "Shared model",
            "active": 1,
            "waiting": 1,
            "limit": 1,
        }
        assert entered.is_set() is False

        time.sleep(0.5)
        holders = redis_client.zrange(key, 0, -1)
        assert len(holders) == 1
        assert redis_client.zscore(key, holders[0]) > time.time() * 1000
        assert redis_client.pttl(key) > 0
        active_stat = next(
            item for item in limiter_b.runtime_stats() if item["model_id"] == model_id
        )
        assert active_stat["active"] == 1

        release_a()
        worker.join(timeout=2)
        assert entered.is_set() is True
        assert redis_client.zcard(key) == 1
        release_b[0]()
        assert redis_client.zcard(key) == 0

        now_ms = int(time.time() * 1000)
        redis_client.zadd(key, {"dead-holder": now_ms - 1000})
        release_expired = limiter_a.acquire(model_id, 1, "Shared model")
        assert redis_client.zscore(key, "dead-holder") is None
        assert redis_client.zcard(key) == 1
        release_expired()
        assert redis_client.zcard(key) == 0
    finally:
        release_a()
        if worker.is_alive():
            worker.join(timeout=2)
        for release in release_b:
            release()


def test_redis_limiter_fails_open_when_backend_is_unavailable(redis_client) -> None:
    import redis

    from app.services import model_limiter

    port = _free_port()
    client = redis.Redis(
        host="127.0.0.1",
        port=port,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    try:
        limiter = model_limiter._RedisModelLimiter(client, poll_seconds=0.01)
        release = limiter.acquire(f"offline-{uuid.uuid4()}", 1, "Offline model")
        assert callable(release)
        release()
    finally:
        client.close()


def test_model_limiter_selects_redis_backend_from_env(monkeypatch, redis_client) -> None:
    from app.services import model_limiter

    connection = redis_client.connection_pool.connection_kwargs
    monkeypatch.setenv("REDIS_ADDR", f"{connection['host']}:{connection['port']}")
    monkeypatch.setattr(model_limiter, "_redis_backend", None)
    monkeypatch.setattr(model_limiter, "_redis_backend_settings", None)
    model_id = f"env-limiter-{uuid.uuid4()}"
    with model_limiter.background():
        release = model_limiter.acquire(
            {"id": model_id, "name": model_id, "display_name": "Env model", "max_concurrency": 1}
        )
    try:
        stat = next(item for item in model_limiter.runtime_stats() if item["model_id"] == model_id)
        assert stat == {
            "model_id": model_id,
            "name": "Env model",
            "active": 1,
            "waiting": 0,
            "limit": 1,
        }
        assert redis_client.zcard(model_limiter.REDIS_KEY_PREFIX + model_id) == 1
    finally:
        release()
        assert redis_client.zcard(model_limiter.REDIS_KEY_PREFIX + model_id) == 0
        backend = model_limiter._redis_backend
        if backend is not None:
            backend.client.close()
