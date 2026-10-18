"""Opt-in integration coverage for a real Docker Engine daemon."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from app.services import docker_sandbox
from app.services.docker_templates import DockerTemplateClient


pytestmark = pytest.mark.skipif(
    os.environ.get("WEKNORA_DOCKER_INTEGRATION") != "1",
    reason="set WEKNORA_DOCKER_INTEGRATION=1 to exercise a real Docker daemon",
)


def test_real_docker_create_exec_terminal_and_delete() -> None:
    image = os.environ.get("WEKNORA_DOCKER_TEST_IMAGE", "ikc-log-center:latest")
    client = docker_sandbox.DockerSandboxClient(
        {"image": image, "network_mode": "none", "idle_ttl_seconds": 120},
        {"env_vars": {"BASE": "real-base"}},
        sweep_enabled=False,
    )
    sandbox = None
    try:
        sandbox = client.create(timeout_seconds=120)
        assert client.exists(sandbox.sandbox_id) is True

        result = client.exec(
            sandbox,
            "printf 'docker-real-ok:%s\\n' \"$BASE\"",
            timeout=15,
        )
        assert result.exit_code == 0
        assert result.stdout == "docker-real-ok:real-base\n"

        with_env = client.exec(
            sandbox,
            "printf '%s\\n' \"$EXTRA\"",
            timeout=15,
            env={"EXTRA": "extra-value"},
        )
        assert with_env.exit_code == 0
        assert with_env.stdout == "extra-value\n"

        client._run(
            ["stop", "--time", "1", sandbox.sandbox_id],
            operation="Stop",
            timeout=15,
        )
        assert client._running(sandbox.sandbox_id) is False
        restarted = client.exec(sandbox, "printf 'restarted-ok\\n'", timeout=15)
        assert restarted.exit_code == 0
        assert restarted.stdout == "restarted-ok\n"

        client._run(["pause", sandbox.sandbox_id], operation="Pause", timeout=15)
        assert client._state(sandbox.sandbox_id) == "paused"
        unpaused = client.exec(sandbox, "printf 'unpaused-ok\\n'", timeout=15)
        assert unpaused.exit_code == 0
        assert unpaused.stdout == "unpaused-ok\n"

        terminal = client.open_terminal(sandbox, cols=80, rows=24)
        try:
            terminal.stream.sock.settimeout(5)
            terminal.write(b"printf 'pty-real-ok\\n'\n")
            output = b""
            deadline = time.monotonic() + 5
            while b"pty-real-ok" not in output and time.monotonic() < deadline:
                output += terminal.read(4096)
            assert b"pty-real-ok" in output
            terminal.resize(100, 40)
        finally:
            terminal.close()

        assert client.exists(sandbox.sandbox_id) is True
    finally:
        if sandbox is not None:
            client.delete(sandbox)

    if sandbox is not None:
        assert client.exists(sandbox.sandbox_id) is False


def test_real_docker_idle_sweep_reclaims_managed_container() -> None:
    image = os.environ.get("WEKNORA_DOCKER_TEST_IMAGE", "ikc-log-center:latest")
    client = docker_sandbox.DockerSandboxClient(
        {"image": image, "network_mode": "none", "idle_ttl_seconds": 1},
        sweep_enabled=False,
    )
    sandbox = None
    try:
        sandbox = client.create(timeout_seconds=1)
        assert client.exists(sandbox.sandbox_id) is True
        time.sleep(1.5)
        assert client.sweep() >= 1
        assert client.exists(sandbox.sandbox_id) is False
    finally:
        if sandbox is not None:
            client.delete(sandbox)


def test_real_docker_template_catalog_and_pending_pull_failure() -> None:
    source = os.environ.get("WEKNORA_DOCKER_TEST_IMAGE", "ikc-log-center:latest")
    image = f"wechatopenai/weknora-sandbox:integration-{os.getpid()}"
    tagged = subprocess.run(
        ["docker", "tag", source, image],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert tagged.returncode == 0, tagged.stderr
    try:
        client = DockerTemplateClient({"image": image})
        templates = client.list_templates()
        configured = next(item for item in templates if item["id"] == image)
        assert configured["status"] == "ready"
        assert configured["standard"] is True
        ensured = client.ensure_template("standard")
        assert ensured["id"] == image
        assert ensured["status"] == "ready"
        assert all(item["id"] != source for item in templates)
    finally:
        subprocess.run(
            ["docker", "rmi", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    missing = DockerTemplateClient({"image": "invalid image reference"})
    pending = missing.ensure_template("standard")
    assert pending["status"] in {"building", "failed"}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = missing._pending_template()
        if pending["status"] == "failed":
            break
        time.sleep(0.05)
    assert pending["status"] == "failed"
    assert pending["error"]
