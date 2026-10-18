"""Session-scoped Docker/Cube/E2B sandbox binding and shell execution."""

from __future__ import annotations

import base64
import posixpath
import re
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .. import db
from ..utils import loads
from . import docker_sandbox, remote_sandbox


class SessionSandboxError(RuntimeError):
    def __init__(self, code: str, message: str, *, kind: str = "internal") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


_DESKTOP_READY = re.compile(r"^READY ([A-Za-z0-9]{32})$")
_WRITE_FILE_ENCODED_CHUNK = 65536
_WRITE_FILE_MAX_BATCH_BYTES = 96 * 1024
_DESKTOP_ENSURE_COMMAND = r"""
set -u
if [ ! -x /usr/local/bin/start-desktop.sh ]; then
  echo WEKNORA_DESKTOP_UNSUPPORTED >&2
  exit 2
fi
mkdir -p /run/desktop
/usr/local/bin/start-desktop.sh </dev/null >>/run/desktop/start-desktop.log 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  tail -c 2000 /run/desktop/start-desktop.log >&2
  exit 1
fi
secret=$(cat /run/desktop/secret 2>/dev/null) || true
case "$secret" in
  *[!A-Za-z0-9]*|"") echo "desktop secret missing or malformed" >&2; exit 1 ;;
esac
if [ "${#secret}" -ne 32 ]; then
  echo "desktop secret length ${#secret}" >&2
  exit 1
fi
printf 'READY %s\n' "$secret"
"""


def _config_row(tenant_id: int, config_id: str) -> dict[str, Any] | None:
    if not config_id:
        return None
    return db.fetch_one(
        "SELECT * FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (config_id, tenant_id),
    )


def resolve_config(tenant_id: int, session: Mapping[str, Any], config_id: str = "") -> dict[str, Any] | None:
    candidates = [config_id, str(session.get("sandbox_config_id") or "")]
    agent_config = loads(session.get("agent_config"), {})
    if isinstance(agent_config, dict):
        candidates.append(str(agent_config.get("sandbox_config_id") or ""))
    agent_id = str(session.get("agent_id") or "")
    if agent_id:
        agent = db.fetch_one(
            "SELECT config FROM custom_agents WHERE id=? AND deleted_at IS NULL",
            (agent_id,),
        )
        agent_data = loads(agent.get("config"), {}) if agent else {}
        if isinstance(agent_data, dict):
            candidates.append(str(agent_data.get("sandbox_config_id") or ""))
    for candidate in candidates:
        row = _config_row(tenant_id, candidate.strip())
        if row:
            return row
    return None


def _binding(tenant_id: int, session_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT * FROM py_session_sandboxes WHERE tenant_id=? AND session_id=?",
        (tenant_id, session_id),
    )


def _client(
    row: Mapping[str, Any],
) -> tuple[remote_sandbox.RemoteSandboxClient | docker_sandbox.DockerSandboxClient, dict[str, Any]]:
    provider = str(row.get("sandbox_type") or "").strip().lower()
    config_data = loads(row.get("config"), {})
    if not isinstance(config_data, dict):
        config_data = {}
    backend = config_data.get(provider)
    if not isinstance(backend, dict):
        backend = {}
    if provider == "docker":
        try:
            return docker_sandbox.DockerSandboxClient(backend, config_data), config_data
        except ValueError as exc:
            raise SessionSandboxError("sandbox_config_invalid", str(exc), kind="invalid_request") from exc
    if provider not in {"cube", "e2b"}:
        raise SessionSandboxError(
            "terminal_unsupported",
            "sandbox backend does not support remote shell execution",
            kind="unsupported",
        )
    try:
        return remote_sandbox.RemoteSandboxClient(provider, backend, config_data), config_data
    except (ValueError, remote_sandbox.RemoteSandboxError) as exc:
        message = exc.reason() if isinstance(exc, remote_sandbox.RemoteSandboxError) else str(exc)
        raise SessionSandboxError("sandbox_config_invalid", message, kind="invalid_request") from exc


def _from_binding(binding: Mapping[str, Any], config_row: Mapping[str, Any]) -> remote_sandbox.RemoteSandbox:
    return remote_sandbox.RemoteSandbox(
        provider=str(binding.get("provider") or config_row.get("sandbox_type") or ""),
        sandbox_id=str(binding.get("sandbox_id") or ""),
        traffic_token=str(binding.get("traffic_token") or ""),
        envd_token=str(binding.get("envd_token") or ""),
        domain=str(binding.get("domain") or ""),
    )


def ensure(tenant_id: int, session: Mapping[str, Any], *, config_id: str = "", provision: bool = True) -> dict[str, Any]:
    session_id = str(session.get("id") or "")
    if not session_id:
        raise SessionSandboxError("sandbox_not_bound", "session has no live sandbox")
    binding = _binding(tenant_id, session_id)
    config_row = resolve_config(tenant_id, session, config_id)
    if binding:
        existing_config = str(binding.get("config_id") or "")
        if not config_row or not existing_config or existing_config == str(config_row.get("id") or ""):
            provider = str(binding.get("provider") or "").strip().lower()
            if provider != "docker":
                return binding
            check_row = config_row or _config_row(tenant_id, existing_config)
            if not check_row:
                binding = {}
            else:
                try:
                    client, _ = _client(check_row)
                    exists = getattr(client, "exists", None)
                    if not callable(exists) or exists(str(binding.get("sandbox_id") or "")):
                        return binding
                except remote_sandbox.RemoteSandboxError as exc:
                    if exc.kind != "not_found":
                        return binding
                except SessionSandboxError:
                    return binding
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM py_session_sandboxes WHERE tenant_id=? AND session_id=?",
                        (tenant_id, session_id),
                    )
                binding = {}
                config_row = check_row
    if not config_row:
        raise SessionSandboxError("sandbox_not_bound", "session has no live sandbox")
    if not provision:
        raise SessionSandboxError("sandbox_not_bound", "session has no live sandbox")
    client, _ = _client(config_row)
    try:
        sandbox = client.create(timeout_seconds=3600)
    except remote_sandbox.RemoteSandboxError as exc:
        raise SessionSandboxError("sandbox_start_failed", exc.reason(), kind=exc.kind) from exc
    now = db.utcnow()
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO py_session_sandboxes(tenant_id, session_id, config_id, provider, sandbox_id, "
                "traffic_token, envd_token, domain, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, session_id) DO UPDATE SET config_id=excluded.config_id, "
                "provider=excluded.provider, sandbox_id=excluded.sandbox_id, traffic_token=excluded.traffic_token, "
                "envd_token=excluded.envd_token, domain=excluded.domain, updated_at=excluded.updated_at",
                (
                    tenant_id,
                    session_id,
                    str(config_row.get("id") or ""),
                    sandbox.provider,
                    sandbox.sandbox_id,
                    sandbox.traffic_token,
                    sandbox.envd_token,
                    sandbox.domain,
                    now,
                    now,
                ),
            )
    except Exception:
        try:
            client.delete(sandbox)
        except Exception:  # noqa: BLE001 - preserve the database failure
            pass
        raise
    if binding:
        try:
            old_client, _ = _client(
                db.fetch_one(
                    "SELECT * FROM tenant_sandbox_configs WHERE id=? AND tenant_id=?",
                    (binding.get("config_id"), tenant_id),
                ) or config_row
            )
            old_client.delete(_from_binding(binding, config_row))
        except Exception:  # noqa: BLE001 - the new binding is already durable
            pass
    return _binding(tenant_id, session_id) or {}


def exec_command(
    tenant_id: int,
    session: Mapping[str, Any],
    command: str,
    *,
    work_dir: str = "/workspace",
    timeout: int = 120,
    env: Mapping[str, str] | None = None,
) -> remote_sandbox.RemoteExecResult:
    binding = ensure(tenant_id, session)
    config_row = _config_row(tenant_id, str(binding.get("config_id") or ""))
    if not config_row:
        raise SessionSandboxError("sandbox_config_missing", "sandbox configuration no longer exists")
    client, config_data = _client(config_row)
    merged_env = config_data.get("env_vars")
    values = {str(key): str(value) for key, value in merged_env.items()} if isinstance(merged_env, dict) else {}
    values.update({str(key): str(value) for key, value in (env or {}).items()})
    target = work_dir.strip() or "/workspace"
    script = f"cd -- {shlex.quote(target)} && {command}"
    try:
        return client.exec(
            _from_binding(binding, config_row),
            script,
            timeout=max(1, min(int(timeout or 120), 600)),
            env=values,
        )
    except remote_sandbox.RemoteSandboxError as exc:
        raise SessionSandboxError("sandbox_exec_failed", exc.reason(), kind=exc.kind) from exc


def stat_file(tenant_id: int, session: Mapping[str, Any], path: str) -> dict[str, Any]:
    """Inspect a sandbox path without following its final symlink component."""
    command = (
        "LC_ALL=C stat -c '%F|%s' -- "
        + shlex.quote(path)
        + " 2>/dev/null || printf 'missing|0\\n'"
    )
    result = exec_command(
        tenant_id,
        session,
        command,
        work_dir="/workspace",
        timeout=30,
    )
    first = result.stdout.splitlines()[0] if result.stdout.strip() else ""
    kind_text, separator, size_text = first.partition("|")
    if not separator:
        raise SessionSandboxError(
            "sandbox_file_stat_failed",
            "sandbox returned an invalid file status",
            kind="internal",
        )
    value = kind_text.strip().lower()
    if value.startswith("regular file") or value.startswith("regular empty file"):
        kind = "file"
    elif value == "directory":
        kind = "directory"
    elif value == "symbolic link":
        kind = "symlink"
    elif value == "missing":
        kind = "missing"
    else:
        kind = "other"
    try:
        size = max(0, int(size_text.strip() or "0"))
    except ValueError as exc:
        raise SessionSandboxError(
            "sandbox_file_stat_failed",
            "sandbox returned an invalid file size",
            kind="internal",
        ) from exc
    return {"kind": kind, "size": size, "type": kind_text.strip()}


def read_file(tenant_id: int, session: Mapping[str, Any], path: str) -> bytes:
    """Read a sandbox file as bytes through the active execution backend."""
    result = exec_command(
        tenant_id,
        session,
        "base64 < " + shlex.quote(path),
        work_dir="/workspace",
        timeout=120,
    )
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise SessionSandboxError(
            "sandbox_file_read_failed",
            "sandbox could not read the file" + (f": {detail}" if detail else ""),
            kind="unavailable",
        )
    try:
        return base64.b64decode("".join(result.stdout.split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise SessionSandboxError(
            "sandbox_file_read_failed",
            "sandbox returned invalid file data",
            kind="internal",
        ) from exc


def write_file(
    tenant_id: int,
    session: Mapping[str, Any],
    path: str,
    data: bytes,
) -> None:
    """Write bytes to a sandbox path atomically through base64 shell chunks."""
    parent = posixpath.dirname(path) or "/"
    temp_path = posixpath.join(parent, f".weknora-write-{uuid.uuid4().hex}.tmp")
    encoded = base64.b64encode(data).decode("ascii")
    commands = [
        "mkdir -p -- " + shlex.quote(parent),
        ": > " + shlex.quote(temp_path),
    ]
    for offset in range(0, len(encoded), _WRITE_FILE_ENCODED_CHUNK):
        chunk = encoded[offset : offset + _WRITE_FILE_ENCODED_CHUNK]
        commands.append(
            "printf %s "
            + shlex.quote(chunk)
            + " | base64 -d >> "
            + shlex.quote(temp_path)
        )
    commands.extend(
        [
            "if [ -d "
            + shlex.quote(path)
            + " ]; then rm -f -- "
            + shlex.quote(temp_path)
            + '; printf "target is a directory\\n" >&2; exit 2; fi',
            "mv -f -- " + shlex.quote(temp_path) + " " + shlex.quote(path),
        ]
    )

    batch: list[str] = []
    batch_bytes = 0
    batches: list[list[str]] = []
    for command in commands:
        command_bytes = len(command.encode("utf-8")) + 4
        if batch and batch_bytes + command_bytes > _WRITE_FILE_MAX_BATCH_BYTES:
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(command)
        batch_bytes += command_bytes
    if batch:
        batches.append(batch)

    for group in batches:
        result = exec_command(
            tenant_id,
            session,
            " &&\n".join(group),
            work_dir="/workspace",
            timeout=120,
        )
        if result.exit_code == 0:
            continue
        try:
            exec_command(
                tenant_id,
                session,
                "rm -f -- " + shlex.quote(temp_path),
                work_dir="/workspace",
                timeout=30,
            )
        except SessionSandboxError:
            pass
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise SessionSandboxError(
            "sandbox_file_write_failed",
            "sandbox could not write the file"
            + (f": {detail}" if detail else ""),
            kind="unavailable",
        )


def list_files(tenant_id: int, session: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    """Walk a sandbox directory and return regular files sorted by path."""
    command = (
        "set -u\n"
        f"target={shlex.quote(path)}\n"
        'if [ ! -e "$target" ]; then exit 0; fi\n'
        'if [ -L "$target" ] || [ ! -d "$target" ]; then '
        'printf "not a directory\\n" >&2; exit 2; fi\n'
        "LC_ALL=C find \"$target\" -type f -printf '%p\\0%s\\0%T@\\0'\n"
    )
    result = exec_command(
        tenant_id,
        session,
        command,
        work_dir="/workspace",
        timeout=60,
    )
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise SessionSandboxError(
            "sandbox_file_list_failed",
            "sandbox could not list the directory" + (f": {detail}" if detail else ""),
            kind="unavailable",
        )
    parts = result.stdout.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) % 3:
        raise SessionSandboxError(
            "sandbox_file_list_failed",
            "sandbox returned an invalid directory listing",
            kind="internal",
        )
    entries: list[dict[str, Any]] = []
    for index in range(0, len(parts), 3):
        file_path, size_text, modified_text = parts[index : index + 3]
        try:
            size = max(0, int(size_text))
            modified = datetime.fromtimestamp(float(modified_text), timezone.utc)
        except (ValueError, OSError) as exc:
            raise SessionSandboxError(
                "sandbox_file_list_failed",
                "sandbox returned invalid file metadata",
                kind="internal",
            ) from exc
        entries.append(
            {
                "name": posixpath.basename(file_path),
                "path": file_path,
                "size": size,
                "modified_at": modified.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
            }
        )
    entries.sort(key=lambda entry: entry["path"])
    return entries


def ensure_desktop(
    tenant_id: int,
    session: Mapping[str, Any],
    *,
    provision: bool = True,
) -> tuple[dict[str, Any], str]:
    binding = ensure(tenant_id, session, provision=provision)
    if str(binding.get("provider") or "").lower() == "docker":
        raise SessionSandboxError(
            "desktop_unsupported",
            "Docker backend does not expose desktop sessions",
            kind="unsupported",
        )
    result = exec_command(tenant_id, session, _DESKTOP_ENSURE_COMMAND, timeout=90)
    if result.exit_code == 2 and "WEKNORA_DESKTOP_UNSUPPORTED" in result.stderr:
        raise SessionSandboxError(
            "desktop_unsupported",
            "sandbox image does not provide a desktop",
            kind="unsupported",
        )
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise SessionSandboxError(
            "desktop_start_failed",
            "desktop failed to start" + (f": {detail}" if detail else ""),
            kind="unavailable",
        )
    ready = _DESKTOP_READY.fullmatch(result.stdout.replace("\r\n", "\n").rstrip("\n"))
    if not ready:
        raise SessionSandboxError(
            "desktop_start_failed",
            "desktop did not report a valid websockify secret",
            kind="internal",
        )
    return binding, ready.group(1)


def desktop_endpoint(
    tenant_id: int,
    session: Mapping[str, Any],
    secret: str,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    binding = _binding(tenant_id, str(session.get("id") or ""))
    if not binding:
        raise SessionSandboxError("sandbox_not_bound", "session has no live sandbox")
    config_row = _config_row(tenant_id, str(binding.get("config_id") or ""))
    if not config_row:
        raise SessionSandboxError("sandbox_config_missing", "sandbox configuration no longer exists")
    client, _ = _client(config_row)
    try:
        url, headers = client.desktop_target(_from_binding(binding, config_row), secret)
    except remote_sandbox.RemoteSandboxError as exc:
        raise SessionSandboxError("desktop_upstream", exc.reason(), kind=exc.kind) from exc
    return binding, url, headers


async def _open_websocket(url: str, headers: dict[str, str]):
    import websockets

    return await websockets.connect(
        url,
        additional_headers=headers,
        subprotocols=["binary"],
        compression=None,
        open_timeout=10,
        ping_interval=30,
        ping_timeout=30,
        max_size=1024 * 1024,
        proxy=None,
    )


async def open_desktop(
    tenant_id: int,
    session: Mapping[str, Any],
    *,
    provision: bool = True,
):
    _, secret = ensure_desktop(tenant_id, session, provision=provision)
    binding, url, headers = desktop_endpoint(tenant_id, session, secret)
    try:
        connection = await _open_websocket(url, headers)
    except Exception as exc:  # noqa: BLE001 - normalize provider socket failures
        raise SessionSandboxError(
            "desktop_upstream",
            f"desktop websocket unavailable: {exc}",
            kind="unavailable",
        ) from exc
    return binding, connection


def open_terminal(
    tenant_id: int,
    session: Mapping[str, Any],
    *,
    provision: bool = True,
    cols: int = 80,
    rows: int = 24,
    pty_id: int = 0,
    work_dir: str = "/workspace",
):
    binding = ensure(tenant_id, session, provision=provision)
    config_row = _config_row(tenant_id, str(binding.get("config_id") or ""))
    if not config_row:
        raise SessionSandboxError("sandbox_config_missing", "sandbox configuration no longer exists")
    client, config_data = _client(config_row)
    opener = getattr(client, "open_terminal", None)
    if not callable(opener):
        raise SessionSandboxError(
            "terminal_unsupported",
            "sandbox backend does not support interactive terminals",
            kind="unsupported",
        )
    env_vars = config_data.get("env_vars")
    values = {str(key): str(value) for key, value in env_vars.items()} if isinstance(env_vars, dict) else {}
    kwargs: dict[str, Any] = {
        "cols": max(1, min(int(cols or 80), 500)),
        "rows": max(1, min(int(rows or 24), 500)),
        "cwd": work_dir.strip() or "/workspace",
        "env": values,
    }
    if pty_id > 0:
        kwargs["attach_pid"] = int(pty_id)
    try:
        terminal = opener(_from_binding(binding, config_row), **kwargs)
    except remote_sandbox.RemoteSandboxError as exc:
        raise SessionSandboxError("sandbox_terminal_failed", exc.reason(), kind=exc.kind) from exc
    return binding, terminal


def delete(tenant_id: int, session_id: str) -> None:
    binding = _binding(tenant_id, session_id)
    if not binding:
        return
    config_row = _config_row(tenant_id, str(binding.get("config_id") or ""))
    if config_row:
        try:
            client, _ = _client(config_row)
            client.delete(_from_binding(binding, config_row))
        except (SessionSandboxError, remote_sandbox.RemoteSandboxError):
            pass
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM py_session_sandboxes WHERE tenant_id=? AND session_id=?",
            (tenant_id, session_id),
        )
