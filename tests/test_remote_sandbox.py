from __future__ import annotations

import base64
import json

import pytest

from app.services import remote_sandbox


def _json_frame(event: dict) -> bytes:
    return remote_sandbox._connect_envelope(json.dumps(event).encode())


def _e2b_response(stdout: str, exit_code: int = 0) -> bytes:
    data = remote_sandbox._field_bytes(1, stdout.encode())
    data_event = remote_sandbox._field_bytes(2, data)
    end = remote_sandbox._field_varint(1, remote_sandbox._zigzag(exit_code))
    end += remote_sandbox._field_varint(2, 1)
    end_event = remote_sandbox._field_bytes(3, end)
    process_event = data_event + end_event
    start_response = remote_sandbox._field_bytes(1, process_event)
    return remote_sandbox._connect_envelope(start_response) + b"\x02\x00\x00\x00\x00"


def test_cube_create_exec_and_delete() -> None:
    calls: list[tuple[str, str, dict[str, str], bytes]] = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, dict(headers), body))
        if url.endswith("/health"):
            return remote_sandbox.HTTPResponse(200, b'{"status":"ok"}')
        if method == "POST" and url.endswith("/sandboxes"):
            return remote_sandbox.HTTPResponse(
                201,
                b'{"sandboxID":"sbx-1","trafficAccessToken":"traffic-1","envdAccessToken":"envd-1"}',
            )
        if url.endswith("/process.Process/Start"):
            request = json.loads(body[5:])
            assert request["process"]["cmd"] == "/bin/bash"
            assert request["process"]["args"][-1] == "echo weknora-ok"
            assert request["stdin"] is False
            stdout = base64.b64encode(b"weknora-ok\n").decode()
            frames = (
                _json_frame({"event": {"start": {"pid": 1}}})
                + _json_frame({"event": {"data": {"stdout": stdout}}})
                + _json_frame({"event": {"end": {"exitCode": 0, "exited": True}}})
                + b"\x02\x00\x00\x00\x00"
            )
            return remote_sandbox.HTTPResponse(200, frames)
        if method == "DELETE" and url.endswith("/sandboxes/sbx-1"):
            return remote_sandbox.HTTPResponse(204, b"")
        raise AssertionError((method, url))

    client = remote_sandbox.RemoteSandboxClient(
        "cube",
        {"api_url": "https://cube.test", "api_key": "key", "template_id": "tpl-1", "proxy_url": "https://gateway.test"},
        {"network": {"deny_egress_by_default": False}},
        transport=transport,
    )
    client.health()
    sandbox = client.create()
    result = client.exec(sandbox, "echo weknora-ok")
    client.delete(sandbox)

    assert result.stdout == "weknora-ok\n"
    assert result.exit_code == 0
    assert calls[0][2]["Authorization"] == "Bearer key"
    create_body = json.loads(calls[1][3])
    assert create_body["templateID"] == "tpl-1"
    assert create_body["network"]["allowPublicTraffic"] is False
    exec_headers = calls[2][2]
    assert exec_headers["Host"] == "49983-sbx-1.cube.app"
    assert exec_headers["e2b-traffic-access-token"] == "traffic-1"
    assert exec_headers["X-Access-Token"] == "envd-1"
    assert calls[3][0] == "DELETE"


def test_desktop_target_uses_websockify_port_and_backend_credentials() -> None:
    client = remote_sandbox.RemoteSandboxClient(
        "cube",
        {"api_url": "https://cube.test", "api_key": "key", "template_id": "tpl-1", "proxy_url": "https://gateway.test"},
        {},
    )
    url, headers = client.desktop_target(
        remote_sandbox.RemoteSandbox("cube", "sbx-1", "traffic-1", "envd-1", "cube.app"),
        "a" * 32,
    )
    assert url == "wss://gateway.test:443/websockify"
    assert headers["Host"] == "6080-sbx-1.cube.app"
    assert headers["e2b-traffic-access-token"] == "traffic-1"
    assert headers["X-Access-Token"] == "envd-1"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"weknora:" + b"a" * 32).decode()

    direct = remote_sandbox.RemoteSandboxClient(
        "e2b",
        {"api_url": "https://api.e2b.test", "api_key": "key", "template_id": "tpl-2", "sandbox_domain": "e2b.test"},
        {},
    )
    direct_url, direct_headers = direct.desktop_target(
        remote_sandbox.RemoteSandbox("e2b", "sbx-2", "traffic-2", "envd-2", ""),
        "b" * 32,
    )
    assert direct_url == "wss://6080-sbx-2.e2b.test/websockify"
    assert direct_headers["Host"] == "6080-sbx-2.e2b.test"


def test_e2b_create_exec_and_delete() -> None:
    calls: list[tuple[str, str, dict[str, str], bytes]] = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, dict(headers), body))
        if method == "GET" and url.endswith("/v2/sandboxes?limit=1"):
            return remote_sandbox.HTTPResponse(200, b"[]")
        if method == "POST" and url.endswith("/sandboxes"):
            return remote_sandbox.HTTPResponse(
                201,
                b'{"sandboxID":"sbx-2","trafficAccessToken":"traffic-2","envdAccessToken":"envd-2"}',
            )
        if url.endswith("/process.Process/Start"):
            return remote_sandbox.HTTPResponse(200, _e2b_response("weknora-ok\n"))
        if method == "DELETE" and url.endswith("/sandboxes/sbx-2"):
            return remote_sandbox.HTTPResponse(204, b"")
        raise AssertionError((method, url))

    client = remote_sandbox.RemoteSandboxClient(
        "e2b",
        {"api_url": "https://api.e2b.test", "api_key": "key", "template_id": "tpl-2", "sandbox_domain": "e2b.test"},
        {},
        transport=transport,
    )
    client.health()
    sandbox = client.create()
    result = client.exec(sandbox, "echo weknora-ok")
    client.delete(sandbox)

    assert result.stdout == "weknora-ok\n"
    assert result.exit_code == 0
    assert calls[0][2]["X-API-Key"] == "key"
    create_body = json.loads(calls[1][3])
    assert create_body["templateID"] == "tpl-2"
    assert create_body["secure"] is True
    assert create_body["allow_internet_access"] is True
    exec_headers = calls[2][2]
    assert exec_headers["Content-Type"] == "application/connect+proto"
    assert exec_headers["Host"] == "49983-sbx-2.e2b.test"
    assert exec_headers["X-Access-Token"] == "envd-2"

    frame = calls[2][3]
    assert frame[0] == 0
    fields = list(remote_sandbox._fields(frame[5:]))
    assert any(number == 1 and wire == 2 for number, wire, _ in fields)
    assert any(number == 4 and wire == 0 and value == 0 for number, wire, value in fields)


def test_exec_error_surfaces_provider_reason() -> None:
    def transport(method, url, headers, body, timeout):
        return remote_sandbox.HTTPResponse(401, b'{"message":"bad key"}')

    client = remote_sandbox.RemoteSandboxClient(
        "cube",
        {"api_url": "https://cube.test", "api_key": "bad", "template_id": "tpl-1"},
        transport=transport,
    )
    with pytest.raises(remote_sandbox.RemoteSandboxError) as caught:
        client.create()
    assert caught.value.kind == "authentication"
    assert "认证失败" in caught.value.reason()


def test_cube_template_catalog_normalizes_and_hides_snapshots() -> None:
    def transport(method, url, headers, body, timeout):
        assert method == "GET"
        assert url == "https://cube.test/templates"
        assert headers["Authorization"] == "Bearer key"
        return remote_sandbox.HTTPResponse(
            200,
            json.dumps([
                {
                    "templateID": "tpl-standard",
                    "status": "READY",
                    "imageInfo": "wechatopenai/weknora-sandbox:main-cube",
                },
                {
                    "templateID": "tpl-desktop",
                    "status": "PENDING",
                    "imageInfo": "wechatopenai/weknora-sandbox:main-desktop-cube",
                },
                {
                    "templateID": "snap-hidden",
                    "status": "READY",
                    "imageInfo": "scratch:latest",
                },
                {
                    "templateID": "tpl-other",
                    "name": "python",
                    "status": "FAILED",
                    "imageInfo": "python:3.11",
                    "lastError": "pull denied",
                },
            ]).encode(),
        )

    client = remote_sandbox.RemoteSandboxClient(
        "cube",
        {"api_url": "https://cube.test", "api_key": "key"},
        transport=transport,
    )
    templates = client.list_templates()

    assert [item["id"] for item in templates] == [
        "tpl-standard", "tpl-desktop", "tpl-other"
    ]
    assert templates[0]["standard"] is True
    assert templates[0]["name"] == "weknora"
    assert templates[1]["desktop"] is True
    assert templates[1]["status"] == "building"
    assert templates[1]["name"] == "weknora-desktop"
    assert templates[2]["status"] == "failed"
    assert templates[2]["error"] == "pull denied"
    assert remote_sandbox.pick_template(templates, "standard")["id"] == "tpl-standard"
    assert remote_sandbox.pick_template(templates, "desktop")["id"] == "tpl-desktop"


def test_e2b_template_catalog_uses_names_aliases_and_build_status() -> None:
    def transport(method, url, headers, body, timeout):
        assert method == "GET"
        assert url == "https://api.e2b.test/templates"
        assert headers["X-API-Key"] == "key"
        return remote_sandbox.HTTPResponse(
            200,
            json.dumps([
                {
                    "templateID": "tmpl-standard",
                    "names": ["project/weknora"],
                    "buildStatus": "ready",
                    "buildID": "build-1",
                },
                {
                    "templateID": "tmpl-desktop",
                    "aliases": ["team/weknora-desktop"],
                    "buildStatus": "in_progress",
                },
            ]).encode(),
        )

    client = remote_sandbox.RemoteSandboxClient(
        "e2b",
        {"api_url": "https://api.e2b.test", "api_key": "key"},
        transport=transport,
    )
    templates = client.list_templates()

    assert templates[0]["standard"] is True
    assert templates[0]["status"] == "ready"
    assert templates[0]["version"] == "build-1"
    assert templates[1]["desktop"] is True
    assert templates[1]["status"] == "building"


def test_e2b_catalog_reconciles_stale_build_status() -> None:
    def transport(method, url, headers, body, timeout):
        assert method == "GET"
        if url == "https://api.e2b.test/templates":
            return remote_sandbox.HTTPResponse(
                200,
                json.dumps([
                    {
                        "templateID": "tmpl-standard",
                        "names": ["weknora"],
                        "buildID": "build-1",
                        "buildStatus": "waiting",
                    }
                ]).encode(),
            )
        if url == "https://api.e2b.test/templates/tmpl-standard/builds/build-1/status":
            return remote_sandbox.HTTPResponse(
                200, b'{"templateID":"tmpl-standard","buildID":"build-1","status":"ready"}'
            )
        raise AssertionError(url)

    client = remote_sandbox.RemoteSandboxClient(
        "e2b",
        {"api_url": "https://api.e2b.test", "api_key": "key"},
        transport=transport,
    )

    assert client.list_templates()[0]["status"] == "ready"


def test_e2b_catalog_marks_finished_but_untagged_build() -> None:
    def transport(method, url, headers, body, timeout):
        if url == "https://api.e2b.test/templates":
            return remote_sandbox.HTTPResponse(
                200,
                json.dumps([
                    {
                        "templateID": "tmpl-standard",
                        "names": ["weknora"],
                        "buildID": "00000000-0000-0000-0000-000000000000",
                        "buildStatus": "waiting",
                        "buildCount": 2,
                    }
                ]).encode(),
            )
        if url == "https://api.e2b.test/templates/tmpl-standard":
            return remote_sandbox.HTTPResponse(
                200,
                json.dumps({
                    "templateID": "tmpl-standard",
                    "builds": [
                        {"buildID": "build-1", "status": "success"},
                        {"buildID": "build-2", "status": "success"},
                    ],
                }).encode(),
            )
        raise AssertionError(url)

    client = remote_sandbox.RemoteSandboxClient(
        "e2b",
        {"api_url": "https://api.e2b.test", "api_key": "key"},
        transport=transport,
    )

    assert client.list_templates()[0]["status"] == "untagged"


def test_cube_ensure_template_rebuilds_failed_template_in_place() -> None:
    calls: list[tuple[str, str, dict]] = []

    def transport(method, url, headers, body, timeout):
        if method == "GET" and url.endswith("/templates"):
            return remote_sandbox.HTTPResponse(
                200,
                json.dumps([
                    {
                        "templateID": "tpl-failed",
                        "aliases": ["weknora"],
                        "status": "FAILED",
                        "imageInfo": remote_sandbox.DEFAULT_CUBE_IMAGE,
                    }
                ]).encode(),
            )
        if method == "POST" and url.endswith("/templates/tpl-failed"):
            calls.append((method, url, json.loads(body)))
            return remote_sandbox.HTTPResponse(
                202, b'{"templateID":"tpl-failed","status":"PENDING"}'
            )
        raise AssertionError((method, url))

    client = remote_sandbox.RemoteSandboxClient(
        "cube",
        {
            "api_url": "https://cube.test",
            "api_key": "key",
            "dns_servers": ["8.8.8.8", "8.8.8.8", "1.1.1.1"],
        },
        transport=transport,
    )
    template = client.ensure_template("standard")

    assert template["id"] == "tpl-failed"
    assert template["status"] == "building"
    assert calls[0][2]["dns"] == ["8.8.8.8", "1.1.1.1"]
    assert calls[0][2]["exposedPorts"] == [remote_sandbox.ENVD_PORT]


def test_e2b_ensure_template_builds_default_tagged_image() -> None:
    calls: list[tuple[str, str, dict]] = []

    def transport(method, url, headers, body, timeout):
        if method == "GET" and url.endswith("/templates"):
            return remote_sandbox.HTTPResponse(200, b"[]")
        if method == "POST" and url.endswith("/v3/templates"):
            calls.append((method, url, json.loads(body)))
            return remote_sandbox.HTTPResponse(
                202,
                b'{"templateID":"tpl-new","buildID":"build-new","names":["weknora"]}',
            )
        if method == "POST" and url.endswith("/v2/templates/tpl-new/builds/build-new"):
            calls.append((method, url, json.loads(body)))
            return remote_sandbox.HTTPResponse(202, b"")
        raise AssertionError((method, url))

    client = remote_sandbox.RemoteSandboxClient(
        "e2b",
        {"api_url": "https://api.e2b.test", "api_key": "key"},
        transport=transport,
    )
    template = client.ensure_template("standard", replace=True)

    assert template["id"] == "tpl-new"
    assert template["status"] == "building"
    assert template["standard"] is True
    assert calls[0][2]["alias"] == "weknora"
    assert calls[0][2]["tags"] == ["default"]
    assert calls[1][2]["fromImage"] == remote_sandbox.DEFAULT_E2B_IMAGE
    assert calls[1][2]["steps"][0]["args"] == [remote_sandbox.E2B_PTY_PROMPT_OVERRIDE]


@pytest.mark.parametrize(
    ("network", "expected"),
    [
        ({}, False),
        ({"deny_egress_by_default": True}, True),
        ({"deny_out": ["10.0.0.0/8"]}, False),
        ({"deny_out": ["0.0.0.0/0"]}, True),
        ({"deny_out": ["0/0"]}, True),
    ],
)
def test_denies_egress_by_default(network: dict, expected: bool) -> None:
    assert remote_sandbox.denies_egress_by_default({"network": network}) is expected


def test_deep_check_creates_executes_and_destroys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import infra

    events: list[str] = []

    class FakeClient:
        def __init__(self, provider, backend, config_data):
            events.append("client")

        def create(self, *, timeout_seconds=120):
            events.append("create")
            return remote_sandbox.RemoteSandbox("cube", "sbx-1")

        def exec(self, sandbox, command, *, timeout=30, env=None):
            if command.startswith("echo "):
                events.append("exec:probe")
                return remote_sandbox.RemoteExecResult("weknora-ok\n", "", 0, 3)
            events.append("exec:egress")
            return remote_sandbox.RemoteExecResult("cn:baidu\n", "", 0, 4)

        def delete(self, sandbox):
            events.append("delete")

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    checks = infra._remote_sandbox_deep_check("cube", {"api_url": "https://cube.test"}, {})
    by_name = {item["name"]: item for item in checks}

    assert by_name["template_exists"]["ok"] is True
    assert by_name["sandbox_exec"]["ok"] is True
    assert by_name["egress_available"]["ok"] is True
    assert events == ["client", "create", "exec:probe", "exec:egress", "delete"]


def test_deep_check_skips_egress_when_policy_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import infra

    deleted: list[bool] = []

    class FakeClient:
        def __init__(self, provider, backend, config_data):
            pass

        def create(self, *, timeout_seconds=120):
            return remote_sandbox.RemoteSandbox("cube", "sbx-1")

        def exec(self, sandbox, command, *, timeout=30, env=None):
            if command.startswith("echo "):
                return remote_sandbox.RemoteExecResult("weknora-ok\n", "", 0, 1)
            return remote_sandbox.RemoteExecResult("", "curl: (28) timeout", 1, 1)

        def delete(self, sandbox):
            deleted.append(True)

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    checks = infra._remote_sandbox_deep_check(
        "cube",
        {"api_url": "https://cube.test"},
        {"network": {"deny_egress_by_default": True}},
    )
    by_name = {item["name"]: item for item in checks}

    assert by_name["egress_available"]["ok"] is None
    assert by_name["egress_available"]["reason"] == "egress_restricted_by_policy"
    assert deleted == [True]
