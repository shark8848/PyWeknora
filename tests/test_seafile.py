import json
import posixpath
import urllib.parse

import pytest

from app.services import seafile


REPO = "11111111-2222-3333-4444-555555555555"
OTHER_REPO = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CONFIG = {
    "base_url": "https://seafile.test",
    "api_token": "secret-token",
    "resource_ids": [REPO + ":/docs"],
    "settings": {},
}


class FakeSeafile:
    def __init__(self) -> None:
        self.repos = [
            {"id": REPO, "name": "Zulu", "type": "repo", "encrypted": False},
            {"id": OTHER_REPO, "name": "Alpha", "type": "srepo", "encrypted": False},
            {"id": "99999999-8888-7777-6666-555555555555", "name": "Hidden", "type": "repo", "encrypted": True},
        ]
        self.dirs: set[tuple[str, str]] = {(repo["id"], "/") for repo in self.repos}
        self.files: dict[tuple[str, str], dict[str, object]] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.failures: dict[tuple[str, str], tuple[int, str, int]] = {}
        self.tokens: dict[str, tuple[str, str]] = {}

    def failure(self, op: str, path: str) -> tuple[int, str] | None:
        value = self.failures.get((op, path))
        if value is None:
            return None
        status, retry_after, remaining = value
        if remaining > 0:
            remaining -= 1
            if remaining == 0:
                self.failures.pop((op, path), None)
            else:
                self.failures[(op, path)] = (status, retry_after, remaining)
        return status, retry_after

    def add_dir(self, repo_id: str, path: str) -> None:
        self.dirs.add((repo_id, path))

    def add_file(self, repo_id: str, path: str, body: bytes, mtime: int = 1) -> None:
        parent = posixpath.dirname(path)
        while parent:
            self.dirs.add((repo_id, parent))
            if parent == "/":
                break
            parent = posixpath.dirname(parent)
        self.files[(repo_id, path)] = {
            "body": body,
            "oid": "oid-" + str(len(self.files) + 1),
            "mtime": mtime,
            "size": len(body),
        }

    def remove(self, repo_id: str, path: str) -> None:
        self.files.pop((repo_id, path), None)

    def entries(self, repo_id: str, path: str) -> list[dict[str, object]]:
        result = []
        for prefix, is_dir in [
            *((value, True) for value in self.dirs if value[0] == repo_id),
            *((value, False) for value in self.files if value[0] == repo_id),
        ]:
            value = prefix[1]
            if value == path or posixpath.dirname(value) != path:
                continue
            name = posixpath.basename(value)
            if is_dir:
                result.append({"id": "dir-" + name, "name": name, "type": "dir", "mtime": 1})
            else:
                file = self.files[prefix]
                result.append(
                    {
                        "id": file["oid"],
                        "name": name,
                        "type": "file",
                        "mtime": file["mtime"],
                        "size": file["size"],
                    }
                )
        return sorted(result, key=lambda item: str(item["name"]))

    def __call__(self, method, url, *, headers=None, timeout=30, max_bytes=0, **_kwargs):
        del method, timeout, max_bytes
        self.calls.append((url, dict(headers or {})))
        parsed = urllib.parse.urlsplit(url)
        if parsed.path == "/api2/auth/ping/":
            return self._status("ping", "", 200, json.dumps("pong").encode())
        if parsed.path == "/api2/repos/":
            return self._status("repos", "", 200, json.dumps(self.repos).encode())
        if parsed.path.startswith("/files/"):
            token = parsed.path.rsplit("/", 1)[-1]
            repo_id, path = self.tokens[token]
            file = self.files[(repo_id, path)]
            return 200, {}, bytes(file["body"]), url
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api2", "repos"] and parts[3] in {"dir", "file"}:
            repo_id, op = parts[2], parts[3]
            path = urllib.parse.parse_qs(parsed.query).get("p", [""])[0]
            status, _retry, code = 200, "", 0
            failure = self.failure(op, path)
            if failure:
                status, retry_after = failure
                return status, {"retry-after": retry_after} if retry_after else {}, b"failure", url
            if op == "dir":
                if (repo_id, path) not in self.dirs:
                    return 404, {}, b"missing", url
                return 200, {}, json.dumps(self.entries(repo_id, path)).encode(), url
            file = self.files.get((repo_id, path))
            if not file:
                return 404, {}, b"missing", url
            token = "token-" + str(len(self.tokens) + 1)
            self.tokens[token] = (repo_id, path)
            return 200, {"oid": str(file["oid"])}, json.dumps(
                "https://files.test/files/" + token
            ).encode(), url
        raise AssertionError(url)

    def _status(self, op: str, path: str, status: int, body: bytes):
        failure = self.failure(op, path)
        if failure:
            status, retry_after = failure
            return status, {"retry-after": retry_after} if retry_after else {}, b"failure", "https://seafile.test/"
        return status, {}, body, "https://seafile.test/"


@pytest.fixture
def fake(monkeypatch) -> FakeSeafile:
    value = FakeSeafile()
    monkeypatch.setattr(seafile.url_guard, "request_url", value)
    monkeypatch.setattr(seafile, "_sleep", lambda _delay: None)
    return value


def test_resource_ids_paths_and_ancestors() -> None:
    assert seafile._parse_resource_id(REPO + ":/产品/100% 文档") == (REPO, "/产品/100% 文档")
    assert seafile._normalise_path("//a///b/") == "/a/b"
    with pytest.raises(seafile.SeafileError):
        seafile._normalise_path("/a/../b")
    repo, roots = seafile._collapse_roots(
        [REPO + ":/docs", REPO + ":/docs/sub", REPO + ":/ab", REPO + ":/docs"]
    )
    assert repo == REPO
    assert roots == ["/ab", "/docs"]
    assert seafile.resource_ancestors([REPO + ":/a/b/c.md"]) == [
        REPO + ":/",
        REPO + ":/a",
        REPO + ":/a/b",
    ]
    assert seafile._knowledge_file_name("Library", "/docs/guide.md") == "Library/docs/guide.md"


def test_lists_libraries_files_and_validates_without_directory_scan(fake: FakeSeafile) -> None:
    fake.add_dir(REPO, "/docs")
    fake.add_dir(REPO, "/docs/sub")
    fake.add_file(REPO, "/docs/guide.md", b"guide")
    fake.add_file(REPO, "/docs/archive.zip", b"zip")

    libraries = seafile.list_resources(CONFIG)
    assert [(item["name"], item["type"]) for item in libraries] == [
        ("Alpha", "library"),
        ("Zulu", "library"),
    ]
    assert libraries[1]["external_id"] == REPO + ":/"

    parent = REPO + ":/docs"
    resources = seafile.list_resources(CONFIG, parent)
    assert [(item["name"], item["type"]) for item in resources] == [
        ("guide.md", "file"),
        ("sub", "directory"),
    ]
    assert resources[0]["external_id"] == REPO + ":/docs/guide.md"

    fake.calls.clear()
    assert seafile.validate(CONFIG)["valid"] is True
    assert [urllib.parse.urlsplit(url).path for url, _headers in fake.calls] == [
        "/api2/auth/ping/",
        "/api2/repos/",
    ]
    assert all(headers.get("Authorization") == "Token secret-token" for _url, headers in fake.calls)


def test_fetch_recurses_skips_unchanged_and_reconciles_deletions(fake: FakeSeafile) -> None:
    fake.add_file(REPO, "/docs/a.md", b"# A\n\nhello")
    fake.add_file(REPO, "/docs/sub/b.md", b"world")

    items = seafile.fetch_items(CONFIG)
    by_path = {item["metadata"]["seafile_path"]: item for item in items}
    assert set(by_path) == {"/docs/a.md", "/docs/sub/b.md"}
    assert by_path["/docs/a.md"]["title"] == "Zulu/docs/a.md"
    assert by_path["/docs/a.md"]["file_name"] == "Zulu/docs/a.md"
    assert by_path["/docs/a.md"]["file_type"] == "md"
    assert "hello" in by_path["/docs/a.md"]["content"]
    assert by_path["/docs/a.md"]["fingerprint"].startswith("o:oid-1|m:1|s:")

    previous = {
        "connector_cursor": {
            "items": {item["external_id"]: item["fingerprint"] for item in items},
            "resources": {},
        }
    }
    assert seafile.fetch_items(CONFIG, previous) == []

    fake.remove(REPO, "/docs/sub/b.md")
    deleted = seafile.fetch_items(CONFIG, previous)
    assert len(deleted) == 1
    assert deleted[0]["is_deleted"] is True
    assert deleted[0]["external_id"] == "seafile:" + REPO + ":/docs/sub/b.md"

    switched = {**CONFIG, "resource_ids": [OTHER_REPO + ":/docs"]}
    with pytest.raises(seafile.SeafileError, match="different Seafile library"):
        seafile.fetch_items(switched, previous)


def test_fetch_full_redownloads_and_cursor_binds_repository(fake: FakeSeafile) -> None:
    fake.add_file(REPO, "/docs/a.md", b"# A")
    cursor_meta: dict[str, object] = {}
    items = seafile.fetch_items(CONFIG, cursor_meta=cursor_meta)
    assert cursor_meta == {"repo_id": REPO, "repo_name": "Zulu"}

    previous = {
        "connector_cursor": {
            **cursor_meta,
            "items": {item["external_id"]: item["fingerprint"] for item in items},
            "resources": {},
        }
    }
    assert seafile.fetch_items(CONFIG, previous) == []

    full = seafile.fetch_items(CONFIG, previous, force_full=True)
    assert [item["metadata"]["seafile_path"] for item in full] == ["/docs/a.md"]


def test_empty_library_cursor_rejects_switch_before_http(fake: FakeSeafile) -> None:
    cursor_meta: dict[str, object] = {}
    assert seafile.fetch_items({**CONFIG, "resource_ids": [REPO + ":/"]}, cursor_meta=cursor_meta) == []
    previous = {"connector_cursor": {**cursor_meta, "items": {}, "resources": {}}}
    fake.calls.clear()
    switched = {**CONFIG, "resource_ids": [OTHER_REPO + ":/"]}

    with pytest.raises(seafile.SeafileError, match="different Seafile library"):
        seafile.fetch_items(switched, previous)
    assert fake.calls == []


def test_fetch_reports_safe_error_codes_and_omits_download_credentials(fake: FakeSeafile) -> None:
    fake.add_file(REPO, "/docs/locked.md", b"locked")
    fake.add_file(REPO, "/docs/empty.md", b"")
    fake.add_file(REPO, "/docs/changed.md", b"abc")
    fake.files[(REPO, "/docs/changed.md")]["size"] = 4
    fake.failures[("file", "/docs/locked.md")] = (403, "", 0)

    items = seafile.fetch_items(CONFIG)
    by_path = {item["metadata"]["seafile_path"]: item for item in items}
    assert by_path["/docs/locked.md"]["metadata"]["error_reason_code"] == seafile.REASON_PERMISSION_DENIED
    assert by_path["/docs/empty.md"]["metadata"]["error_reason_code"] == seafile.REASON_EMPTY_FILE
    assert by_path["/docs/changed.md"]["metadata"]["error_reason_code"] == seafile.REASON_SOURCE_CHANGED
    download_calls = [headers for url, headers in fake.calls if url.startswith("https://files.test/")]
    assert download_calls
    assert all("Authorization" not in headers for headers in download_calls)


def test_retries_transient_responses_and_rejects_long_retry_after(fake: FakeSeafile, monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(seafile, "_sleep", waits.append)
    fake.failures[("ping", "")] = (503, "", 2)
    assert seafile.validate({**CONFIG, "resource_ids": []})["valid"] is True
    assert waits == [1.0, 2.0]

    fake.failures[("ping", "")] = (429, "120", 1)
    with pytest.raises(seafile.SeafileError, match="more than 60 seconds"):
        seafile.validate({**CONFIG, "resource_ids": []})
