"""Docker image catalog and background pulls for sandbox templates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping


DEFAULT_IMAGE = "wechatopenai/weknora-sandbox:main"
STANDARD_REPOSITORY = "wechatopenai/weknora-sandbox"
TEMPLATE_LABEL = "com.weknora.sandbox.template"
SNAPSHOT_LABELS = (
    "com.weknora.sandbox.skill-snapshot",
    "com.weknora.sandbox.fork-snapshot",
)
PULL_BUDGET_SECONDS = 30 * 60


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
        timeout=timeout,
        env=dict(env),
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _normalize_repository(image: str) -> str:
    reference = image.strip()
    if not reference:
        return ""
    reference = reference.split("@", 1)[0]
    colon = reference.rfind(":")
    if colon > reference.rfind("/"):
        reference = reference[:colon]
    parts = [part for part in reference.strip("/").split("/") if part]
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    if len(parts) > 1 and parts[0].lower() == "library":
        parts = parts[1:]
    return "/".join(parts).lower()


def _is_standard(image: str) -> bool:
    return _normalize_repository(image) == STANDARD_REPOSITORY


def _label_map(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    labels: dict[str, str] = {}
    for pair in str(raw or "").split(","):
        key, separator, value = pair.partition("=")
        if separator and key.strip():
            labels[key.strip()] = value.strip()
    return labels


def _is_snapshot(repository: str, labels: Mapping[str, str]) -> bool:
    if any(labels.get(label) == "true" for label in SNAPSHOT_LABELS):
        return True
    repository = repository.strip().lower()
    return repository.startswith(("weknora-skill/", "weknora-fork/", "docker.io/weknora-skill/", "docker.io/weknora-fork/"))


_pull_lock = threading.Lock()
_pulls: dict[str, dict[str, Any]] = {}


class DockerTemplateClient:
    def __init__(
        self,
        backend: Mapping[str, Any],
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.backend = dict(backend)
        self.image = str(backend.get("image") or DEFAULT_IMAGE).strip() or DEFAULT_IMAGE
        self.host = str(backend.get("host") or "").strip()
        self.timeout = float(backend.get("http_timeout_sec") or 30)
        self.runner = runner or _default_runner
        self.env = dict(os.environ)
        cert_path = str(backend.get("tls_cert_path") or "").strip()
        if cert_path:
            self.env["DOCKER_CERT_PATH"] = cert_path
            self.env["DOCKER_TLS_VERIFY"] = "1"

    def _argv(self, args: list[str]) -> list[str]:
        docker = shutil.which("docker")
        if not docker:
            raise ValueError("Docker CLI is not available")
        argv = [docker]
        if self.host and self.host not in {"unix:///var/run/docker.sock"}:
            argv.extend(["-H", self.host])
        return [*argv, *args]

    def _run(self, args: list[str], *, timeout: float | None = None) -> str:
        try:
            result = self.runner(self._argv(args), self.env, float(timeout or self.timeout))
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Docker command timed out") from exc
        except OSError as exc:
            raise ValueError("Docker CLI is not available") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"docker exited {result.returncode}").strip()
            raise ValueError(detail[:500])
        return result.stdout

    def _inspect(self, image: str) -> bool:
        try:
            result = self.runner(
                self._argv(["image", "inspect", image, "--format", "{{.Id}}"]),
                self.env,
                self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _pull_key(self) -> str:
        return f"{self.host or 'local'}|{self.image}"

    def _pending_template(self) -> dict[str, Any]:
        template: dict[str, Any] = {
            "id": self.image,
            "name": self.image,
            "image": self.image,
            "status": "missing",
            "standard": _is_standard(self.image),
        }
        with _pull_lock:
            state = _pulls.get(self._pull_key())
        if state is None:
            return template
        if not state.get("done"):
            template["status"] = "building"
        elif state.get("error"):
            template["status"] = "failed"
            template["error"] = str(state["error"])
        return template

    def _start_pull(self) -> None:
        key = self._pull_key()
        with _pull_lock:
            state = _pulls.get(key)
            if state is not None and not state.get("done"):
                return
            state = {"done": False, "error": ""}
            _pulls[key] = state

        def pull() -> None:
            error = ""
            try:
                result = self.runner(
                    self._argv(["pull", self.image]), self.env, PULL_BUDGET_SECONDS
                )
                if result.returncode != 0:
                    error = (
                        result.stderr or result.stdout or f"docker pull exited {result.returncode}"
                    ).strip()[:500]
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                error = str(exc)[:500]
            with _pull_lock:
                if error:
                    state["done"] = True
                    state["error"] = error
                else:
                    _pulls.pop(key, None)

        threading.Thread(target=pull, name="docker-template-pull", daemon=True).start()

    def list_templates(self) -> list[dict[str, Any]]:
        output = self._run(["image", "ls", "--no-trunc", "--format", "{{json .}}"])
        templates: list[dict[str, Any]] = []
        seen: set[str] = set()
        configured_present = False
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            repository = str(item.get("Repository") or "").strip()
            tag = str(item.get("Tag") or "").strip()
            if not repository or not tag or repository == "<none>" or tag == "<none>":
                continue
            image = f"{repository}:{tag}"
            if image in seen:
                continue
            labels = _label_map(item.get("Labels"))
            snapshot = _is_snapshot(repository, labels)
            standard = _is_standard(image)
            if snapshot and image != self.image:
                continue
            if (
                not standard
                and not snapshot
                and labels.get(TEMPLATE_LABEL) != "true"
                and image != self.image
            ):
                continue
            seen.add(image)
            if image == self.image:
                configured_present = True
            templates.append({
                "id": image,
                "name": image,
                "status": "ready",
                "image": image,
                "version": str(item.get("ID") or "").strip(),
                "created_at": str(item.get("CreatedAt") or "").strip(),
                "standard": standard,
            })
        if not configured_present:
            pending = self._pending_template()
            if pending["id"] not in seen:
                templates.append(pending)
        return templates

    def ensure_template(self, kind: str, *, replace: bool = False) -> dict[str, Any]:
        if kind != "standard":
            raise ValueError("Docker backend does not expose desktop templates")
        if self._inspect(self.image):
            return {
                "id": self.image,
                "name": self.image,
                "image": self.image,
                "status": "ready",
                "standard": _is_standard(self.image),
            }
        self._start_pull()
        if self._inspect(self.image):
            return {
                "id": self.image,
                "name": self.image,
                "image": self.image,
                "status": "ready",
                "standard": _is_standard(self.image),
            }
        template = self._pending_template()
        if template["status"] == "missing":
            template["status"] = "building"
        return template

    def delete_superseded_templates(self, kind: str, keep_id: str) -> None:
        return None
