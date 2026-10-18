from __future__ import annotations

import json
import threading
import time

from app.services import docker_templates


def _json_lines(items: list[dict]) -> str:
    return "\n".join(json.dumps(item) for item in items)


def test_docker_template_catalog_filters_images_and_hides_snapshots(monkeypatch) -> None:
    monkeypatch.setattr(docker_templates.shutil, "which", lambda _: "/usr/bin/docker")
    items = [
        {
            "ID": "sha256:standard",
            "Repository": "wechatopenai/weknora-sandbox",
            "Tag": "main",
            "CreatedAt": "2026-10-01 10:00:00 +0800 CST",
            "Labels": "",
        },
        {
            "ID": "sha256:desktop",
            "Repository": "wechatopenai/weknora-sandbox",
            "Tag": "main-desktop",
            "CreatedAt": "2026-10-02 10:00:00 +0800 CST",
            "Labels": "",
        },
        {
            "ID": "sha256:snapshot",
            "Repository": "weknora-skill/tenant",
            "Tag": "snap-1",
            "Labels": "",
        },
        {
            "ID": "sha256:labeled-snapshot",
            "Repository": "registry.local/snapshot",
            "Tag": "v1",
            "Labels": "com.weknora.sandbox.skill-snapshot=true",
        },
        {
            "ID": "sha256:custom",
            "Repository": "registry.local/team/custom",
            "Tag": "v1",
            "Labels": "com.weknora.sandbox.template=true",
        },
        {
            "ID": "sha256:postgres",
            "Repository": "postgres",
            "Tag": "16",
            "Labels": "",
        },
    ]

    def runner(argv, env, timeout):
        assert argv[-5:-1] == ["image", "ls", "--no-trunc", "--format"]
        return docker_templates.CommandResult(0, _json_lines(items))

    client = docker_templates.DockerTemplateClient(
        {"image": "wechatopenai/weknora-sandbox:main"}, runner=runner
    )
    templates = client.list_templates()

    assert [item["id"] for item in templates] == [
        "wechatopenai/weknora-sandbox:main",
        "wechatopenai/weknora-sandbox:main-desktop",
        "registry.local/team/custom:v1",
    ]
    assert all(item["standard"] for item in templates[:2])
    assert templates[2]["standard"] is False


def test_docker_template_catalog_reports_configured_missing_image(monkeypatch) -> None:
    monkeypatch.setattr(docker_templates.shutil, "which", lambda _: "/usr/bin/docker")

    def runner(argv, env, timeout):
        return docker_templates.CommandResult(
            0,
            _json_lines([
                {
                    "ID": "sha256:standard",
                    "Repository": "wechatopenai/weknora-sandbox",
                    "Tag": "main",
                    "Labels": "",
                }
            ]),
        )

    client = docker_templates.DockerTemplateClient(
        {"image": "registry.local/team/missing:v1"}, runner=runner
    )
    templates = client.list_templates()
    pending = next(item for item in templates if item["id"] == "registry.local/team/missing:v1")

    assert pending["status"] == "missing"
    assert pending["standard"] is False


def test_docker_ensure_template_reports_ready_for_present_image(monkeypatch) -> None:
    monkeypatch.setattr(docker_templates.shutil, "which", lambda _: "/usr/bin/docker")
    calls: list[list[str]] = []

    def runner(argv, env, timeout):
        calls.append(argv)
        return docker_templates.CommandResult(0, "sha256:present")

    client = docker_templates.DockerTemplateClient(
        {"image": "wechatopenai/weknora-sandbox:main"}, runner=runner
    )
    template = client.ensure_template("standard")

    assert template["status"] == "ready"
    assert template["standard"] is True
    assert len(calls) == 1
    assert calls[0][1:4] == ["image", "inspect", "wechatopenai/weknora-sandbox:main"]


def test_docker_ensure_template_starts_background_pull(monkeypatch) -> None:
    monkeypatch.setattr(docker_templates.shutil, "which", lambda _: "/usr/bin/docker")
    started = threading.Event()
    release = threading.Event()

    def runner(argv, env, timeout):
        if "inspect" in argv:
            return docker_templates.CommandResult(1)
        if "pull" in argv:
            started.set()
            release.wait(2)
            return docker_templates.CommandResult(0)
        raise AssertionError(argv)

    client = docker_templates.DockerTemplateClient(
        {"image": "registry.local/team/pulled:v1"}, runner=runner
    )
    template = client.ensure_template("standard")

    assert started.wait(2)
    assert template["status"] == "building"
    release.set()
    key = "local|registry.local/team/pulled:v1"
    deadline = time.time() + 2
    while key in docker_templates._pulls and time.time() < deadline:
        time.sleep(0.01)
    assert key not in docker_templates._pulls
