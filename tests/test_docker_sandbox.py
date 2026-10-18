from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

from app.services import docker_sandbox


def _env_file_values(argv: list[str]) -> dict[str, str]:
    path = argv[argv.index("--env-file") + 1]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path, encoding="utf-8") as handle:
        return dict(line.rstrip("\n").split("=", 1) for line in handle if line.strip())


def test_docker_sandbox_creates_executes_and_removes_persistent_container() -> None:
    calls: list[list[str]] = []
    environment_paths: list[str] = []
    container_id = "c" * 64

    def runner(argv, env, timeout):
        calls.append(argv)
        if "run" in argv:
            assert _env_file_values(argv) == {"BASE": "1"}
            environment_paths.append(argv[argv.index("--env-file") + 1])
            assert "--network" in argv
            assert argv[argv.index("--network") + 1] == "none"
            assert f"{docker_sandbox.MANAGED_LABEL}=true" in argv
            assert f"{docker_sandbox.IDLE_TTL_LABEL}=900" in argv
            assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
            assert argv[-3] == "wechatopenai/weknora-sandbox:test"
            return docker_sandbox.CommandResult(0, container_id + "\n")
        if "inspect" in argv:
            return docker_sandbox.CommandResult(0, "running\n")
        if "exec" in argv:
            assert _env_file_values(argv) == {"EXTRA": "2"}
            environment_paths.append(argv[argv.index("--env-file") + 1])
            assert argv[-5] == "/bin/sh"
            assert "timeout -s KILL 12 /bin/bash --noprofile --norc -c" in argv[-3]
            assert argv[-1] == "echo $EXTRA"
            return docker_sandbox.CommandResult(7, "out\n", "err\n")
        if "rm" in argv:
            assert argv[-1] == container_id
            return docker_sandbox.CommandResult(0, container_id + "\n")
        raise AssertionError(argv)

    client = docker_sandbox.DockerSandboxClient(
        {"image": "wechatopenai/weknora-sandbox:test", "cpu_limit": 1.5},
        {"env_vars": {"BASE": "1"}, "network": {"allow_internet_access": False}},
        runner=runner,
        sweep_enabled=False,
    )
    sandbox = client.create(timeout_seconds=900)
    assert sandbox.provider == "docker"
    assert sandbox.sandbox_id == container_id

    result = client.exec(sandbox, "echo $EXTRA", timeout=12, env={"EXTRA": "2"})
    assert (result.stdout, result.stderr, result.exit_code) == ("out\n", "err\n", 7)

    client.delete(sandbox)
    assert environment_paths
    assert all(not os.path.exists(path) for path in environment_paths)
    assert calls[0][0].endswith("docker")


def test_docker_sandbox_restarts_stopped_container_before_exec() -> None:
    state = {"running": False}
    calls: list[str] = []

    def runner(argv, env, timeout):
        if "inspect" in argv:
            calls.append("inspect")
            return docker_sandbox.CommandResult(
                0, "running\n" if state["running"] else "exited\n"
            )
        if "start" in argv:
            calls.append("start")
            state["running"] = True
            return docker_sandbox.CommandResult(0, "container\n")
        if "exec" in argv:
            calls.append("exec")
            return docker_sandbox.CommandResult(0, "ok\n")
        raise AssertionError(argv)

    client = docker_sandbox.DockerSandboxClient({}, runner=runner, sweep_enabled=False)
    result = client.exec(
        docker_sandbox.RemoteSandbox(provider="docker", sandbox_id="container"),
        "true",
    )
    assert result.exit_code == 0
    assert calls == ["inspect", "start", "inspect", "exec"]


def test_docker_sandbox_unpauses_paused_container_before_exec() -> None:
    state = {"status": "paused"}
    calls: list[str] = []

    def runner(argv, env, timeout):
        if "inspect" in argv:
            calls.append("inspect")
            return docker_sandbox.CommandResult(0, state["status"] + "\n")
        if "unpause" in argv:
            calls.append("unpause")
            state["status"] = "running"
            return docker_sandbox.CommandResult(0, "container\n")
        if "exec" in argv:
            calls.append("exec")
            return docker_sandbox.CommandResult(0, "ok\n")
        raise AssertionError(argv)

    client = docker_sandbox.DockerSandboxClient({}, runner=runner, sweep_enabled=False)
    result = client.exec(
        docker_sandbox.RemoteSandbox(provider="docker", sandbox_id="container"),
        "true",
    )
    assert result.exit_code == 0
    assert calls == ["inspect", "unpause", "inspect", "exec"]


def test_docker_sandbox_reclassifies_exec_race_with_idle_sweep() -> None:
    inspections = 0

    def runner(argv, env, timeout):
        nonlocal inspections
        if "inspect" in argv:
            inspections += 1
            if inspections == 1:
                return docker_sandbox.CommandResult(0, "running\n")
            return docker_sandbox.CommandResult(
                1, stderr="Error: No such object: swept-away"
            )
        if "exec" in argv:
            return docker_sandbox.CommandResult(1, "", "")
        raise AssertionError(argv)

    client = docker_sandbox.DockerSandboxClient({}, runner=runner, sweep_enabled=False)
    try:
        client.exec(
            docker_sandbox.RemoteSandbox(provider="docker", sandbox_id="swept-away"),
            "true",
        )
    except docker_sandbox.RemoteSandboxError as exc:
        assert exc.kind == "not_found"
        assert "exit=1" in exc.message
    else:
        raise AssertionError("a vanished container must be classified as not_found")
    assert inspections == 2


def test_docker_sandbox_keeps_genuine_exec_failure_when_container_alive() -> None:
    def runner(argv, env, timeout):
        if "inspect" in argv:
            return docker_sandbox.CommandResult(0, "running\n")
        if "exec" in argv:
            return docker_sandbox.CommandResult(1, stderr="command failed\n")
        raise AssertionError(argv)

    client = docker_sandbox.DockerSandboxClient({}, runner=runner, sweep_enabled=False)
    result = client.exec(
        docker_sandbox.RemoteSandbox(provider="docker", sandbox_id="alive"),
        "false",
    )
    assert (result.exit_code, result.stderr) == (1, "command failed\n")


def test_docker_sandbox_delete_ignores_missing_container() -> None:
    def runner(argv, env, timeout):
        return docker_sandbox.CommandResult(1, stderr="Error: No such container: missing")

    client = docker_sandbox.DockerSandboxClient({}, runner=runner, sweep_enabled=False)
    client.delete(docker_sandbox.RemoteSandbox(provider="docker", sandbox_id="missing"))


def test_docker_sandbox_exists_treats_missing_object_as_not_found() -> None:
    def runner(argv, env, timeout):
        return docker_sandbox.CommandResult(
            1, stderr="error: no such object: missing"
        )

    client = docker_sandbox.DockerSandboxClient({}, runner=runner, sweep_enabled=False)
    assert client.exists("missing") is False


def test_docker_sandbox_rejects_shared_network_modes() -> None:
    for mode in ("host", "container:db", "deployment_default"):
        try:
            docker_sandbox.DockerSandboxClient(
                {"network_mode": mode}, runner=lambda *_: None, sweep_enabled=False
            )
        except ValueError as exc:
            assert "bridge or none" in str(exc)
        else:
            raise AssertionError(mode)


def test_docker_sandbox_sweep_reclaims_only_idle_managed_containers() -> None:
    now = datetime.now(timezone.utc)
    removed: list[str] = []
    started = "2020-01-01T00:00:00Z"

    def labels(ttl: int) -> str:
        return f'{{"{docker_sandbox.MANAGED_LABEL}":"true","{docker_sandbox.IDLE_TTL_LABEL}":"{ttl}"}}'

    def runner(argv, env, timeout):
        if "ps" in argv:
            return docker_sandbox.CommandResult(0, "idle\nfresh\nlong-ttl\nunmanaged\n")
        if "inspect" in argv:
            container = argv[-1]
            if container == "unmanaged":
                return docker_sandbox.CommandResult(0, f'{{}}|{started}|{started}')
            ttl = 600 if container == "long-ttl" else 60
            return docker_sandbox.CommandResult(
                0, f"{labels(ttl)}|{started}|{started}"
            )
        if "exec" in argv:
            container = argv[argv.index("exec") + 1]
            timestamp = now - timedelta(seconds=10 if container == "fresh" else 120)
            return docker_sandbox.CommandResult(0, f"{int(timestamp.timestamp())}\n")
        if "rm" in argv:
            removed.append(argv[-1])
            return docker_sandbox.CommandResult(0, argv[-1])
        raise AssertionError(argv)

    client = docker_sandbox.DockerSandboxClient(
        {}, runner=runner, sweep_enabled=False
    )
    assert client.sweep(now=now) == 1
    assert removed == ["idle"]
