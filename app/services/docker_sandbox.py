"""Session-persistent Docker sandbox execution through the Docker CLI."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

from .remote_sandbox import RemoteExecResult, RemoteSandbox, RemoteSandboxError
from .sandbox_terminal import SandboxTerminal, SandboxTerminalClosed


DEFAULT_IMAGE = "wechatopenai/weknora-sandbox:main"
MANAGED_LABEL = "com.weknora.sandbox.managed"
IDLE_TTL_LABEL = "com.weknora.sandbox.idle-ttl-seconds"
ACTIVITY_MARKER = "/var/lib/weknora-sandbox-activity"
DEFAULT_CPU_LIMIT = 2.0
DEFAULT_MEMORY_LIMIT_MB = 2048
DEFAULT_PIDS_LIMIT = 512
CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETGID", "SETUID", "KILL")
SWEEP_MIN_INTERVAL_SECONDS = 60.0
ACTIVITY_CLOCK_SKEW_SECONDS = 120.0
START_READY_TIMEOUT_SECONDS = 5.0
START_READY_POLL_SECONDS = 0.05
TERMINAL_ID_ENV = "WEKNORA_TERMINAL_ID"

logger = logging.getLogger(__name__)

_sweep_lock = threading.Lock()
_sweep_last: dict[str, float] = {}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], Mapping[str, str], float], CommandResult]


def _default_runner(
    argv: list[str], env: Mapping[str, str], timeout: float
) -> CommandResult:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=dict(env),
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _error_kind(detail: str) -> str:
    value = detail.lower()
    if "permission denied" in value or "cannot connect" in value or "connection refused" in value:
        return "unavailable"
    if "is the docker daemon running" in value:
        return "unavailable"
    if "no such container" in value or "no such object" in value or "no such image" in value:
        return "not_found"
    if "conflict" in value or "already in use" in value:
        return "invalid_request"
    return "internal"


def _docker_time(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed if parsed.year > 1970 else None


@contextmanager
def _environment_file(values: Mapping[str, str]) -> Iterator[str | None]:
    if not values:
        yield None
        return
    lines: list[str] = []
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = str(raw_value)
        if not key or "=" in key or "\x00" in key or "\x00" in value or "\n" in value or "\r" in value:
            raise RemoteSandboxError(
                "docker", "Exec", "invalid sandbox environment variable", kind="invalid_request"
            )
        lines.append(f"{key}={value}\n")
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="weknora-docker-env-", delete=False
    )
    try:
        os.chmod(handle.name, 0o600)
        handle.writelines(lines)
        handle.close()
        yield handle.name
    finally:
        if not handle.closed:
            handle.close()
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass


class _SocketReader:
    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self.sock = sock
        self.buffer = bytearray(initial)

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise EOFError("Docker Engine closed the connection")
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def read_until(self, marker: bytes, limit: int = 64 * 1024) -> bytes:
        while marker not in self.buffer:
            if len(self.buffer) >= limit:
                raise RemoteSandboxError(
                    "docker", "Connect", "Docker Engine returned oversized headers", kind="internal"
                )
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("Docker Engine closed the connection")
            self.buffer.extend(chunk)
        end = self.buffer.index(marker) + len(marker)
        value = bytes(self.buffer[:end])
        del self.buffer[:end]
        return value

    def read_all(self) -> bytes:
        chunks = [bytes(self.buffer)]
        self.buffer.clear()
        while True:
            chunk = self.sock.recv(32768)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def _engine_endpoint(host: str) -> tuple[str, str, int | None]:
    value = host.strip() or os.environ.get("DOCKER_HOST", "").strip()
    if not value:
        return "unix", "/var/run/docker.sock", None
    if value.startswith("unix://"):
        return "unix", value.removeprefix("unix://") or "/var/run/docker.sock", None
    if value.startswith("/"):
        return "unix", value, None
    if "://" not in value:
        value = "tcp://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"tcp", "http", "https"} or not parsed.hostname:
        raise RemoteSandboxError(
            "docker",
            "Connect",
            "Docker host must be a unix socket or tcp/http(s) endpoint",
            kind="invalid_request",
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 2375)
    return parsed.scheme, parsed.hostname, port


class DockerEngine:
    """Small standard-library client for the Docker Engine API."""

    def __init__(self, host: str = "", *, cert_path: str = "", timeout: float = 30) -> None:
        self.host = host
        self.cert_path = cert_path
        self.timeout = max(1.0, float(timeout or 30))
        self.api_version = os.environ.get("DOCKER_API_VERSION", "").strip() or "v1.41"

    def _connect(self, timeout: float | None = None) -> socket.socket:
        scheme, host, port = _engine_endpoint(self.host)
        limit = self.timeout if timeout is None else max(0.1, timeout)
        try:
            if scheme == "unix":
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(limit)
                sock.connect(host)
                return sock
            sock = socket.create_connection((host, int(port or 2375)), timeout=limit)
            tls = scheme == "https" or os.environ.get("DOCKER_TLS_VERIFY", "").strip() not in {"", "0"}
            if not tls:
                return sock
            context = ssl.create_default_context(cafile=os.path.join(self.cert_path, "ca.pem") if self.cert_path else None)
            if self.cert_path:
                context.load_cert_chain(
                    os.path.join(self.cert_path, "cert.pem"),
                    os.path.join(self.cert_path, "key.pem"),
                )
            return context.wrap_socket(sock, server_hostname=host)
        except (OSError, ssl.SSLError) as exc:
            raise RemoteSandboxError(
                "docker", "Connect", str(exc) or "Docker Engine is unavailable", kind="unavailable"
            ) from exc

    def _request_bytes(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        upgrade: bool = False,
    ) -> tuple[bytes, bytes]:
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        target = f"/{self.api_version}{path}"
        lines = [
            f"{method} {target} HTTP/1.1",
            "Host: docker",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
        ]
        if upgrade:
            lines.extend(["Connection: Upgrade", "Upgrade: tcp"])
        else:
            lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body, body

    @staticmethod
    def _parse_head(raw: bytes) -> tuple[int, dict[str, str]]:
        try:
            text = raw.decode("iso-8859-1")
            lines = text.split("\r\n")
            parts = lines[0].split(" ", 2)
            status = int(parts[1])
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line or ":" not in line:
                    continue
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            return status, headers
        except (IndexError, ValueError) as exc:
            raise RemoteSandboxError(
                "docker", "Connect", "Docker Engine returned a malformed response", kind="internal"
            ) from exc

    @staticmethod
    def _read_chunked(reader: _SocketReader) -> bytes:
        body = bytearray()
        while True:
            raw_size = reader.read_until(b"\r\n").strip()
            try:
                size = int(raw_size.split(b";", 1)[0], 16)
            except ValueError as exc:
                raise RemoteSandboxError(
                    "docker", "Connect", "Docker Engine returned malformed chunking", kind="internal"
                ) from exc
            if size == 0:
                reader.read_until(b"\r\n")
                return bytes(body)
            body += reader.read_exact(size)
            if reader.read_exact(2) != b"\r\n":
                raise RemoteSandboxError(
                    "docker", "Connect", "Docker Engine returned malformed chunking", kind="internal"
                )

    def _read_body(self, reader: _SocketReader, headers: Mapping[str, str]) -> bytes:
        if headers.get("transfer-encoding", "").lower() == "chunked":
            return self._read_chunked(reader)
        length = headers.get("content-length", "")
        if length:
            try:
                return reader.read_exact(int(length))
            except ValueError as exc:
                raise RemoteSandboxError(
                    "docker", "Connect", "Docker Engine returned an invalid length", kind="internal"
                ) from exc
        return reader.read_all()

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        operation: str = "Connect",
        timeout: float | None = None,
    ) -> bytes:
        sock = self._connect(timeout)
        reader = _SocketReader(sock)
        try:
            request, _ = self._request_bytes(method, path, payload)
            sock.sendall(request)
            raw_head = reader.read_until(b"\r\n\r\n")
            status, headers = self._parse_head(raw_head)
            body = self._read_body(reader, headers)
        except socket.timeout as exc:
            raise RemoteSandboxError("docker", operation, "Docker Engine request timed out", kind="timeout") from exc
        except (OSError, EOFError) as exc:
            raise RemoteSandboxError("docker", operation, str(exc), kind="unavailable") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if status < 200 or status >= 300:
            detail = body.decode("utf-8", errors="replace")
            try:
                decoded = json.loads(detail)
                if isinstance(decoded, dict) and decoded.get("message"):
                    detail = str(decoded["message"])
            except ValueError:
                pass
            detail = detail.strip()[:500] or f"Docker Engine returned HTTP {status}"
            raise RemoteSandboxError(
                "docker", operation, detail, kind=_error_kind(detail) if status >= 400 else "internal"
            )
        return body

    def start_exec(self, exec_id: str, *, detached: bool = False) -> "_DockerStream | None":
        payload = {"Detach": detached, "Tty": not detached}
        if detached:
            self.request("POST", f"/exec/{exec_id}/start", payload, operation="Exec")
            return None
        sock = self._connect()
        reader = _SocketReader(sock)
        try:
            request, _ = self._request_bytes("POST", f"/exec/{exec_id}/start", payload, upgrade=True)
            sock.sendall(request)
            raw_head = reader.read_until(b"\r\n\r\n")
            status, headers = self._parse_head(raw_head)
            if status not in {101, 200}:
                body = self._read_body(reader, headers)
                detail = body.decode("utf-8", errors="replace").strip()[:500]
                raise RemoteSandboxError(
                    "docker", "Exec", detail or f"Docker Engine returned HTTP {status}", kind="internal"
                )
            sock.settimeout(None)
            return _DockerStream(sock, bytes(reader.buffer))
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def create_exec(self, container_id: str, payload: Mapping[str, Any]) -> str:
        body = self.request("POST", f"/containers/{container_id}/exec", payload, operation="Exec")
        try:
            decoded = json.loads(body.decode("utf-8"))
            exec_id = str(decoded.get("Id") or "")
        except (ValueError, AttributeError) as exc:
            raise RemoteSandboxError(
                "docker", "Exec", "Docker Engine returned an invalid exec handle", kind="internal"
            ) from exc
        if not exec_id:
            raise RemoteSandboxError("docker", "Exec", "Docker Engine returned an empty exec handle", kind="internal")
        return exec_id

    def resize_exec(self, exec_id: str, cols: int, rows: int) -> None:
        self.request(
            "POST",
            f"/exec/{exec_id}/resize?h={max(1, int(rows))}&w={max(1, int(cols))}",
            operation="Exec",
        )

    def inspect_exec(self, exec_id: str) -> dict[str, Any]:
        body = self.request("GET", f"/exec/{exec_id}/json", operation="Exec")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise RemoteSandboxError(
                "docker", "Exec", "Docker Engine returned an invalid exec state", kind="internal"
            ) from exc
        return decoded if isinstance(decoded, dict) else {}


class _DockerStream:
    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self.sock = sock
        self.buffer = bytearray(initial)

    def recv(self, size: int = 32768) -> bytes:
        if self.buffer:
            chunk = bytes(self.buffer[:size])
            del self.buffer[:size]
            return chunk
        return self.sock.recv(size)

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _docker_terminal_kill_cmd(token: str) -> list[str]:
    marker = f"{TERMINAL_ID_ENV}={token}"
    script = f'''m="{marker}"
carriers() {{
for d in /proc/[0-9]*; do
e=$(tr '\\0' '\\n' < "$d/environ" 2>/dev/null) || continue
case "$e" in *"$m"*) echo "${{d#/proc/}}" ;; esac
done
}}
pids=$(carriers)
[ -n "$pids" ] || exit 0
kill -HUP $pids 2>/dev/null
kill -CONT $pids 2>/dev/null
sleep 2
for q in $(carriers); do
s=$(cat "/proc/$q/stat" 2>/dev/null) || continue
set -- ${{s##*) }}
[ "$4" = "$q" ] && kill -9 "$q" 2>/dev/null
done
exit 0'''
    return ["/bin/sh", "-c", script, "weknora-terminate", token]


class DockerTerminalSession(SandboxTerminal):
    backend = "docker"

    def __init__(
        self,
        engine: DockerEngine,
        stream: _DockerStream,
        container_id: str,
        exec_id: str,
        terminal_id: str,
    ) -> None:
        self.engine = engine
        self.stream = stream
        self.container_id = container_id
        self.exec_id = exec_id
        self.terminal_id = terminal_id
        self._closed = False
        self._terminated = False
        self._lock = threading.Lock()

    def read(self, size: int = 32768) -> bytes:
        try:
            data = self.stream.recv(size)
        except OSError as exc:
            if self._closed:
                raise SandboxTerminalClosed(-1) from exc
            raise RemoteSandboxError("docker", "terminal stream", str(exc), kind="unavailable") from exc
        if data:
            return data
        if self._closed:
            raise SandboxTerminalClosed(-1)
        raise SandboxTerminalClosed(self._inspect_exit_code())

    def write(self, data: bytes) -> None:
        if not data:
            return
        try:
            self.stream.sendall(data)
        except OSError as exc:
            raise RemoteSandboxError("docker", "terminal input", str(exc), kind="unavailable") from exc

    def resize(self, cols: int, rows: int) -> None:
        if cols <= 0 or rows <= 0:
            return
        self.engine.resize_exec(self.exec_id, cols, rows)

    def _inspect_exit_code(self) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = self.engine.inspect_exec(self.exec_id)
            except RemoteSandboxError:
                return -1
            if not bool(state.get("Running")):
                try:
                    return int(state.get("ExitCode", -1))
                except (TypeError, ValueError):
                    return -1
            time.sleep(0.1)
        return -1

    def _terminate(self) -> None:
        with self._lock:
            if self._terminated:
                return
            self._terminated = True
        try:
            exec_id = self.engine.create_exec(
                self.container_id,
                {"AttachStdin": False, "AttachStdout": False, "AttachStderr": False, "Tty": False,
                 "Cmd": _docker_terminal_kill_cmd(self.terminal_id), "User": "root"},
            )
            self.engine.start_exec(exec_id, detached=True)
        except RemoteSandboxError:
            logger.warning("failed to terminate Docker terminal %s", self.exec_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stream.close()
        self._terminate()


class DockerSandboxClient:
    """One long-lived container per session, matching the Cube/E2B client shape."""

    def __init__(
        self,
        backend: Mapping[str, Any] | None,
        config_data: Mapping[str, Any] | None = None,
        *,
        runner: CommandRunner | None = None,
        sweep_enabled: bool = True,
        engine: DockerEngine | None = None,
    ) -> None:
        self.backend = dict(backend or {})
        self.config_data = dict(config_data or {})
        self.image = str(self.backend.get("image") or DEFAULT_IMAGE).strip() or DEFAULT_IMAGE
        self.host = str(self.backend.get("host") or "").strip()
        self.timeout = float(self.backend.get("http_timeout_sec") or 30)
        self.custom_runner = runner is not None
        self.runner = runner or _default_runner
        self.sweep_enabled = sweep_enabled
        self._engine = engine
        self.docker = shutil.which("docker")
        self.env = dict(os.environ)
        cert_path = str(self.backend.get("tls_cert_path") or "").strip()
        if cert_path:
            self.env["DOCKER_CERT_PATH"] = cert_path
            self.env["DOCKER_TLS_VERIFY"] = "1"
        self.network_mode = self._network_mode()
        try:
            self.cpu_limit = float(self.backend.get("cpu_limit") or DEFAULT_CPU_LIMIT)
            self.memory_limit_mb = int(
                self.backend.get("memory_limit_mb") or DEFAULT_MEMORY_LIMIT_MB
            )
            self.pids_limit = int(self.backend.get("pids_limit") or DEFAULT_PIDS_LIMIT)
            self.idle_ttl = int(self.backend.get("idle_ttl_seconds") or 3600)
        except (TypeError, ValueError) as exc:
            raise ValueError("Docker sandbox resource limits must be numeric") from exc
        if (
            self.cpu_limit <= 0
            or self.memory_limit_mb <= 0
            or self.pids_limit <= 0
            or self.idle_ttl <= 0
        ):
            raise ValueError("Docker sandbox resource limits must be positive")

    def _network_mode(self) -> str:
        mode = str(self.backend.get("network_mode") or "bridge").strip() or "bridge"
        network = self.config_data.get("network")
        if isinstance(network, Mapping) and network.get("allow_internet_access") is False:
            mode = "none"
        if mode not in {"bridge", "none"}:
            raise ValueError("Docker sandbox network_mode must be bridge or none")
        return mode

    def _argv(self, args: list[str]) -> list[str]:
        docker = self.docker
        if not docker:
            if not self.custom_runner:
                raise RemoteSandboxError(
                    "docker", "Command", "Docker CLI is not available", kind="unavailable"
                )
            docker = "docker"
        argv = [docker]
        if self.host and self.host not in {"unix:///var/run/docker.sock"}:
            argv.extend(["-H", self.host])
        return [*argv, *args]

    def _invoke(
        self,
        args: list[str],
        *,
        operation: str,
        timeout: float | None = None,
    ) -> CommandResult:
        try:
            return self.runner(self._argv(args), self.env, float(timeout or self.timeout))
        except subprocess.TimeoutExpired as exc:
            raise RemoteSandboxError(
                "docker", operation, "Docker command timed out", kind="timeout"
            ) from exc
        except OSError as exc:
            raise RemoteSandboxError(
                "docker", operation, str(exc) or "Docker CLI is not available", kind="unavailable"
            ) from exc

    def _run(self, args: list[str], *, operation: str, timeout: float | None = None) -> str:
        result = self._invoke(args, operation=operation, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"docker exited {result.returncode}").strip()
            raise RemoteSandboxError(
                "docker", operation, detail[:500] or "Docker command failed", kind=_error_kind(detail)
            )
        return result.stdout

    def _state(self, sandbox_id: str) -> str:
        output = self._run(
            ["inspect", "--format", "{{.State.Status}}", sandbox_id],
            operation="Connect",
        )
        return output.strip().lower()

    def _running(self, sandbox_id: str) -> bool:
        return self._state(sandbox_id) == "running"

    def exists(self, sandbox_id: str) -> bool:
        result = self._invoke(
            ["inspect", "--format", "{{.Id}}", sandbox_id], operation="Connect"
        )
        if result.returncode == 0:
            return True
        detail = (result.stderr or result.stdout or f"docker exited {result.returncode}").strip()
        if _error_kind(detail) == "not_found":
            return False
        raise RemoteSandboxError(
            "docker", "Connect", detail[:500] or "Docker command failed", kind=_error_kind(detail)
        )

    def _ensure_running(self, sandbox_id: str) -> None:
        status = self._state(sandbox_id)
        if status == "running":
            return
        if status == "paused":
            self._run(["unpause", sandbox_id], operation="Connect")
        else:
            self._run(["start", sandbox_id], operation="Connect")
        deadline = time.monotonic() + START_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._running(sandbox_id):
                return
            time.sleep(START_READY_POLL_SECONDS)
        raise RemoteSandboxError(
            "docker",
            "Connect",
            f"container did not reach running (last state {status!r})",
            kind="timeout",
        )

    def _remove_quietly(self, sandbox_id: str) -> None:
        try:
            self._invoke(["rm", "-f", sandbox_id], operation="Delete")
        except RemoteSandboxError:
            pass

    def _endpoint_key(self) -> str:
        return self.host or "unix:///var/run/docker.sock"

    def _claim_sweep(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        key = self._endpoint_key()
        with _sweep_lock:
            last = _sweep_last.get(key)
            if last is not None and current - last < SWEEP_MIN_INTERVAL_SECONDS:
                return False
            _sweep_last[key] = current
            return True

    def trigger_sweep(self) -> None:
        if not self.sweep_enabled or not self._claim_sweep():
            return

        def run() -> None:
            try:
                reclaimed = self.sweep()
                if reclaimed:
                    logger.info("reclaimed %d idle Docker sandbox container(s)", reclaimed)
            except Exception as exc:  # noqa: BLE001 - reclaim is best effort
                logger.warning("Docker idle sandbox sweep failed: %s", exc)

        threading.Thread(target=run, name="docker-sandbox-sweep", daemon=True).start()

    def _container_activity(self, sandbox_id: str, started_at: datetime | None) -> datetime | None:
        result = self._invoke(
            ["exec", sandbox_id, "stat", "-c", "%Y", ACTIVITY_MARKER],
            operation="Sweep",
        )
        if result.returncode == 0:
            try:
                marker = datetime.fromtimestamp(int(result.stdout.strip()), timezone.utc)
            except (ValueError, OSError):
                marker = None
            if marker is not None:
                if marker <= datetime.now(timezone.utc) + timedelta(
                    seconds=ACTIVITY_CLOCK_SKEW_SECONDS
                ):
                    return marker
        return started_at

    def _container_idle(self, sandbox_id: str, now: datetime) -> bool:
        inspected = self._invoke(
            [
                "inspect", "--format",
                "{{json .Config.Labels}}|{{.State.StartedAt}}|{{.Created}}", sandbox_id,
            ],
            operation="Sweep",
        )
        if inspected.returncode != 0:
            return False
        labels_raw, separator, timestamps = inspected.stdout.strip().partition("|")
        if not separator:
            return False
        try:
            labels = json.loads(labels_raw)
        except (TypeError, ValueError):
            labels = {}
        if not isinstance(labels, dict) or labels.get(MANAGED_LABEL) != "true":
            return False
        try:
            ttl = int(labels.get(IDLE_TTL_LABEL) or self.idle_ttl)
        except (TypeError, ValueError):
            ttl = self.idle_ttl
        if ttl <= 0:
            return False
        started_raw, _, created_raw = timestamps.partition("|")
        last_used = self._container_activity(
            sandbox_id, _docker_time(started_raw) or _docker_time(created_raw)
        )
        return last_used is not None and (now - last_used).total_seconds() > ttl

    def sweep(self, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        output = self._run(
            [
                "ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                "--format", "{{.ID}}",
            ],
            operation="Sweep",
        )
        reclaimed = 0
        for sandbox_id in (line.strip() for line in output.splitlines()):
            if not sandbox_id:
                continue
            try:
                if not self._container_idle(sandbox_id, current):
                    continue
                if not self._container_idle(sandbox_id, datetime.now(timezone.utc)):
                    continue
                result = self._invoke(["rm", "-f", sandbox_id], operation="Sweep")
                if result.returncode == 0 or "no such container" in (
                    result.stderr or result.stdout
                ).lower():
                    reclaimed += 1
            except RemoteSandboxError:
                continue
        return reclaimed

    def create(self, *, timeout_seconds: int = 120) -> RemoteSandbox:
        idle_ttl = max(1, int(timeout_seconds or 120))
        name = "weknora-sandbox-" + uuid.uuid4().hex
        args = [
            "run", "-d", "--name", name,
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{IDLE_TTL_LABEL}={idle_ttl}",
            "--init", "--user", "0", "--network", self.network_mode,
            "--memory", f"{self.memory_limit_mb}m",
            "--memory-swap", f"{self.memory_limit_mb}m",
            "--cpus", str(self.cpu_limit), "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--entrypoint", "/bin/sh", "--workdir", "/workspace",
        ]
        for capability in CAPABILITIES:
            args.extend(["--cap-add", capability])
        runtime = str(self.backend.get("runtime") or "").strip()
        if runtime:
            args.extend(["--runtime", runtime])
        env_vars = self.config_data.get("env_vars")
        values = env_vars if isinstance(env_vars, Mapping) else {}
        sandbox_id = ""
        try:
            with _environment_file(values) as env_file:
                if env_file:
                    args.extend(["--env-file", env_file])
                args.extend([
                    self.image,
                    "-c",
                    f"touch {ACTIVITY_MARKER} 2>/dev/null; "
                    f"chmod 666 {ACTIVITY_MARKER} 2>/dev/null; exec sleep infinity",
                ])
                output = self._run(args, operation="Create", timeout=max(self.timeout, 120))
            sandbox_id = output.strip().splitlines()[-1] if output.strip() else ""
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{12,}", sandbox_id):
                raise RemoteSandboxError(
                    "docker", "Create", "Docker returned an invalid container ID", kind="internal"
                )
            self._ensure_running(sandbox_id)
            self.trigger_sweep()
        except Exception:
            if sandbox_id:
                self._remove_quietly(sandbox_id)
            raise
        return RemoteSandbox(provider="docker", sandbox_id=sandbox_id)

    def open_terminal(
        self,
        sandbox: RemoteSandbox,
        *,
        cols: int = 80,
        rows: int = 24,
        cwd: str = "/workspace",
        attach_pid: int = 0,
        env: Mapping[str, str] | None = None,
    ) -> DockerTerminalSession:
        del attach_pid  # Docker has no exec re-attach; every connect gets a fresh shell.
        self._ensure_running(sandbox.sandbox_id)
        engine = self._engine or DockerEngine(self.host, cert_path=self.env.get("DOCKER_CERT_PATH", ""))
        terminal_id = secrets.token_hex(16)
        values = {
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        values.update({str(key): str(value) for key, value in (env or {}).items()})
        values[TERMINAL_ID_ENV] = terminal_id
        exec_id = engine.create_exec(
            sandbox.sandbox_id,
            {
                "AttachStdin": True,
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": True,
                "Cmd": ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"],
                "WorkingDir": cwd.strip() or "/workspace",
                "User": "root",
                "Env": [f"{key}={value}" for key, value in values.items()],
            },
        )
        stream = engine.start_exec(exec_id)
        if stream is None:
            raise RemoteSandboxError("docker", "Exec", "Docker Engine did not attach the terminal", kind="internal")
        session = DockerTerminalSession(engine, stream, sandbox.sandbox_id, exec_id, terminal_id)
        try:
            session.resize(cols, rows)
        except RemoteSandboxError:
            session.close()
            raise
        self.trigger_sweep()
        return session

    def exec(
        self,
        sandbox: RemoteSandbox,
        command: str,
        *,
        timeout: int = 30,
        env: Mapping[str, str] | None = None,
    ) -> RemoteExecResult:
        self._ensure_running(sandbox.sandbox_id)
        seconds = max(1, int(timeout or 30))
        wrapper = (
            f"touch {ACTIVITY_MARKER} 2>/dev/null || true; "
            f'exec timeout -s KILL {seconds} /bin/bash --noprofile --norc -c "$1"'
        )
        args = ["exec"]
        started = time.perf_counter()
        with _environment_file(env or {}) as env_file:
            if env_file:
                args.extend(["--env-file", env_file])
            args.extend([
                sandbox.sandbox_id, "/bin/sh", "-c", wrapper, "weknora-exec", command
            ])
            result = self._invoke(
                args, operation="Exec", timeout=max(self.timeout, seconds + 5)
            )
        if result.returncode != 0:
            try:
                vanished = not self.exists(sandbox.sandbox_id)
            except RemoteSandboxError:
                vanished = False
            if vanished:
                detail = (result.stderr or result.stdout).strip()
                message = (
                    "container vanished mid-op (likely idle sweep); "
                    f"exit={result.returncode}"
                )
                if detail:
                    message += f": {detail}"
                raise RemoteSandboxError(
                    "docker", "Exec", message[:500], kind="not_found"
                )
        self.trigger_sweep()
        latency = int((time.perf_counter() - started) * 1000)
        return RemoteExecResult(result.stdout, result.stderr, result.returncode, latency)

    def delete(self, sandbox: RemoteSandbox) -> None:
        result = self._invoke(
            ["rm", "-f", sandbox.sandbox_id], operation="Delete"
        )
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or f"docker exited {result.returncode}").strip()
        if "no such container" in detail.lower():
            return
        raise RemoteSandboxError(
            "docker", "Delete", detail[:500] or "Docker command failed", kind=_error_kind(detail)
        )

    def desktop_target(self, sandbox: RemoteSandbox, secret: str) -> tuple[str, dict[str, str]]:
        raise RemoteSandboxError(
            "docker", "Desktop", "Docker backend does not expose desktop sessions", kind="unsupported"
        )
