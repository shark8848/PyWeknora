import io
import json
import urllib.parse
import zipfile

from app.services import dingtalk


CONFIG = {
    "client_id": "ding-app",
    "client_secret": "secret",
    "operator_id": "union-id",
    "resource_ids": ["space"],
    "settings": {},
}


def _response(value):
    return 200, {}, json.dumps(value).encode(), "https://api.dingtalk.com/"


def test_client_uses_official_endpoints_paginates_and_caches_token(monkeypatch) -> None:
    client = dingtalk.DingTalkClient(CONFIG)
    calls = []

    def fake_call(method, path, body, headers):
        calls.append((method, path, body, headers))
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/v1.0/oauth2/accessToken":
            assert json.loads(body) == {"appKey": "ding-app", "appSecret": "secret"}
            return _response({"accessToken": "token", "expireIn": 7200})
        assert headers["x-acs-dingtalk-access-token"] == "token"
        if parsed.path == "/v2.0/wiki/workspaces":
            assert query["maxResults"] == ["30"]
            if query.get("nextToken") == ["page-2"]:
                return _response({"workspaces": [{"workspaceId": "b", "rootNodeId": "root-b", "name": "B"}]})
            return _response({
                "workspaces": [{"workspaceId": "a", "rootNodeId": "root-a", "name": "A"}],
                "nextToken": "page-2",
            })
        if parsed.path == "/v2.0/wiki/nodes":
            assert query["parentNodeId"] == ["root-a"]
            if query.get("nextToken") == ["node-page-2"]:
                return _response({"nodes": [{"nodeId": "doc-2", "type": "FILE", "category": "ALIDOC", "extension": "adoc"}]})
            return _response({
                "nodes": [{"nodeId": "doc-1", "type": "FILE", "category": "ALIDOC", "extension": "adoc"}],
                "nextToken": "node-page-2",
            })
        if parsed.path == "/v1.0/doc/suites/documents/doc%2Fkey/blocks":
            assert query["startIndex"] == ["0"]
            assert query["endIndex"] == ["99"]
            return _response({"success": True, "result": {"data": [{"blockType": "paragraph"}]}})
        raise AssertionError(path)

    monkeypatch.setattr(client, "_call", fake_call)
    workspaces = client.list_workspaces()
    nodes = client.list_nodes("root-a")
    blocks = client.document_blocks("doc/key")

    assert [item.id for item in workspaces] == ["a", "b"]
    assert [item.id for item in nodes] == ["doc-1", "doc-2"]
    assert blocks == [{"blockType": "paragraph"}]
    assert sum(1 for call in calls if call[1] == "/v1.0/oauth2/accessToken") == 1


def test_resource_ids_round_trip_and_reject_cycles() -> None:
    ref = dingtalk.ResourceRef("space", "doc", ("folder", "nested"))
    encoded = dingtalk._encode_ref(ref)
    assert encoded == "dingtalk:v1?ancestor=folder&ancestor=nested&node=doc&workspace=space"
    assert dingtalk._decode_ref(encoded) == ref
    assert dingtalk._decode_ref("space") == dingtalk.ResourceRef("space")

    try:
        dingtalk._encode_ref(dingtalk.ResourceRef("space", "folder", ("folder",)))
    except dingtalk.DingTalkError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("expected a resource cycle error")


def test_render_document_matches_dingtalk_block_shapes() -> None:
    blocks = [
        {"blockType": "heading", "heading": {"level": "heading-2", "text": "Overview"}},
        {"blockType": "paragraph", "children": [
            {"elementType": "text", "text": "bold", "bold": True},
            {"elementType": "text", "text": " and 2 * 3 "},
            {"elementType": "link", "properties": {"href": "https://example.com/a"},
             "children": [{"elementType": "text", "text": "link"}]},
        ]},
        {"blockType": "unorderedList", "unorderedList": {"text": "first item"}},
        {"blockType": "orderedList", "orderedList": {"text": "nested\ncontinuation", "list": {"level": 1}}},
        {"blockType": "table", "table": {"cells": [["A", "B"], ["1", "x|y"]]}},
        {"blockType": "code", "code": {"syntax": "bash", "text": "echo hi"}},
        {"blockType": "unknown", "unknown": {"rawType": "sdt", "text": "contents"}},
    ]
    rendered = dingtalk.render_document("Team | Notes", blocks)

    assert rendered.markdown.startswith("# Team | Notes\n\n## Overview\n\n")
    assert "**bold** and 2 \\* 3 [link](https://example.com/a)" in rendered.markdown
    assert "- first item\n  1. nested\n     continuation\n" in rendered.markdown
    assert "| A | B |\n| --- | --- |\n| 1 | x\\|y |" in rendered.markdown
    assert "```bash\necho hi\n```" in rendered.markdown
    assert "contents" in rendered.markdown
    assert rendered.unknown_types == ["unknown:sdt"]


def test_fetch_items_traverses_folders_and_reconciles_complete_scan(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")
    nodes = {
        "root": [dingtalk.Node("folder", type="FOLDER"), dingtalk.Node("deleted-doc", type="FILE", category="DOCUMENT", extension="pdf")],
        "folder": [dingtalk.Node(
            "doc-1", workspace_id="space", name="Roadmap", type="FILE",
            category="ALIDOC", extension="adoc", modified_timestamp=1_700_000_000_000,
        )],
    }

    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_nodes", lambda self, parent: nodes.get(parent, []))
    monkeypatch.setattr(dingtalk.DingTalkClient, "document_blocks", lambda self, document_id: [
        {"blockType": "paragraph", "children": [{"elementType": "text", "text": "Q3 goals", "bold": True}]},
    ])

    previous = {"connector_cursor": {"resources": {"doc-1": "space", "deleted-doc": "space"}}}
    items = dingtalk.fetch_items(CONFIG, previous)
    by_id = {item["external_id"]: item for item in items}

    assert by_id["doc-1"]["title"] == "Roadmap"
    assert by_id["doc-1"]["content"] == "# Roadmap\n\n**Q3 goals**\n"
    assert by_id["doc-1"]["fingerprint"]
    assert by_id["deleted-doc"]["is_deleted"] is True


def _docx_bytes(text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return output.getvalue()


def test_uploaded_files_download_and_extract_when_enabled(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")
    document = dingtalk.Node(
        "uploaded-1", workspace_id="space", name="Handbook.docx", type="FILE",
        category="DOCUMENT", extension="docx", modified_timestamp=1_700_000_000_000,
    )
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "list_nodes",
        lambda self, parent: [document] if parent == "root" else [],
    )
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "download_document",
        lambda self, document_id: _docx_bytes("Uploaded handbook body"),
    )

    config = {**CONFIG, "settings": {"include_uploaded_files": True}}
    items = dingtalk.fetch_items(config)

    assert items[0]["external_id"] == "uploaded-1"
    assert items[0]["content"].startswith("# Handbook.docx\n\n")
    assert "Uploaded handbook body" in items[0]["content"]
    assert items[0]["metadata"]["category"] == "DOCUMENT"
    assert items[0]["metadata"]["content_type"].endswith("wordprocessingml.document")


def test_uploaded_files_stay_skipped_by_default(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")
    document = dingtalk.Node(
        "uploaded-1", workspace_id="space", name="Handbook.docx", type="FILE",
        category="DOCUMENT", extension="docx",
    )
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "list_nodes",
        lambda self, parent: [document] if parent == "root" else [],
    )
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "download_document",
        lambda self, _document_id: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    assert dingtalk.fetch_items(CONFIG) == []


def test_client_resolves_dentry_and_requests_download_url(monkeypatch) -> None:
    client = dingtalk.DingTalkClient(CONFIG)
    calls = []

    def request_json(method, path, body=None, *, authenticated=True):
        calls.append((method, path, body))
        if "queryDentryId" in path:
            return {"dentryId": "1001", "spaceId": "2002", "dentryUuid": "node-id"}
        return {
            "protocol": "OSS",
            "headerSignatureInfo": {
                "headers": {"Authorization": "signed"},
                "resourceUrls": ["https://objects.example.test/handbook.docx"],
            },
        }

    monkeypatch.setattr(client, "_request_json", request_json)
    ref = client.resolve_dentry("node/id")
    url, headers = client.request_download_url(ref)

    assert ref == {"dentry_id": "1001", "space_id": "2002", "dentry_uuid": "node-id"}
    assert "/v2.0/doc/dentries/node%2Fid/queryDentryId?" in calls[0][1]
    assert calls[1][0] == "POST"
    assert "/v1.0/storage/spaces/2002/dentries/1001/downloadInfos/query?" in calls[1][1]
    assert calls[1][2] == {"option": {"version": 1, "preferIntranet": False}}
    assert url == "https://objects.example.test/handbook.docx"
    assert headers == {"Authorization": "signed"}


def test_client_downloads_signed_object(monkeypatch) -> None:
    client = dingtalk.DingTalkClient(CONFIG)
    monkeypatch.setattr(
        client,
        "resolve_dentry",
        lambda _document_id: {"dentry_id": "1", "space_id": "2", "dentry_uuid": ""},
    )
    monkeypatch.setattr(
        client,
        "request_download_url",
        lambda _ref: ("https://objects.example.test/file", {"Authorization": "signed"}),
    )
    monkeypatch.setattr(
        dingtalk.url_guard,
        "request_url",
        lambda *_args, **_kwargs: (200, {}, b"document bytes", "https://objects.example.test/file"),
    )

    assert client.download_document("doc") == b"document bytes"


def test_validate_probes_uploaded_document_access(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")
    document = dingtalk.Node(
        "uploaded-1", name="Handbook.pdf", type="FILE", category="DOCUMENT", extension="pdf",
    )
    probes: list[str] = []
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "list_nodes_page",
        lambda self, parent, token="": ([document], "") if parent == "root" else ([], ""),
    )
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "verify_document_download",
        lambda self, document_id: probes.append(document_id),
    )

    config = {**CONFIG, "settings": {"include_uploaded_files": True}}
    assert dingtalk.validate(config)["valid"] is True
    assert probes == ["uploaded-1"]


def test_validate_keeps_looking_after_a_failed_document_probe(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")
    pages = {
        ("root", ""): ([
            dingtalk.Node("bad", name="Unreadable", type="FILE", category="ALIDOC", extension="adoc"),
            dingtalk.Node("folder", name="Folder", type="FOLDER"),
        ], ""),
        ("folder", ""): ([
            dingtalk.Node("good", name="Readable", type="FILE", category="ALIDOC", extension="adoc"),
        ], ""),
    }
    probes: list[str] = []
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "list_nodes_page",
        lambda self, parent, token="": pages[(parent, token)],
    )

    def blocks(self, document_id):
        probes.append(document_id)
        if document_id == "bad":
            raise dingtalk.DingTalkError("permission denied")
        return [{"blockType": "paragraph"}]

    monkeypatch.setattr(dingtalk.DingTalkClient, "document_blocks", blocks)

    assert dingtalk.validate(CONFIG) == {"valid": True, "items": 1, "resources": 1}
    assert probes == ["bad", "good"]


def test_validate_accepts_an_inconclusive_folder_walk(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")

    def page(self, parent, token=""):
        if parent == "root":
            return [
                dingtalk.Node("bad", name="Unreadable", type="FILE", category="ALIDOC", extension="adoc"),
                dingtalk.Node("folder", name="Folder", type="FOLDER"),
            ], ""
        raise dingtalk.DingTalkError("folder permission denied")

    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_nodes_page", page)
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "document_blocks",
        lambda self, _document_id: (_ for _ in ()).throw(dingtalk.DingTalkError("permission denied")),
    )

    assert dingtalk.validate(CONFIG)["valid"] is True


def test_validate_reports_the_probe_cap_for_a_conclusive_walk(monkeypatch) -> None:
    workspace = dingtalk.Workspace("space", "root", "Space")
    documents = [
        dingtalk.Node(f"doc-{index}", name=f"Doc {index}", type="FILE", category="ALIDOC", extension="adoc")
        for index in range(6)
    ]
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "list_nodes_page",
        lambda self, parent, token="": (documents, "") if parent == "root" else ([], ""),
    )
    monkeypatch.setattr(
        dingtalk.DingTalkClient,
        "document_blocks",
        lambda self, _document_id: (_ for _ in ()).throw(dingtalk.DingTalkError("permission denied")),
    )

    try:
        dingtalk.validate(CONFIG)
    except dingtalk.DingTalkError as exc:
        assert "only the first 5 documents were probed" in str(exc)
    else:
        raise AssertionError("expected the probe cap to reject the data source")


def test_validate_continues_after_a_workspace_listing_failure(monkeypatch) -> None:
    broken = dingtalk.Workspace("broken", "root-broken", "Broken")
    working = dingtalk.Workspace("working", "root-working", "Working")
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [broken, working])

    def page(self, parent, token=""):
        if parent == "root-broken":
            raise dingtalk.DingTalkError("workspace permission denied")
        return [
            dingtalk.Node("good", name="Readable", type="FILE", category="ALIDOC", extension="adoc"),
        ], ""

    monkeypatch.setattr(dingtalk.DingTalkClient, "list_nodes_page", page)
    monkeypatch.setattr(dingtalk.DingTalkClient, "document_blocks", lambda self, _document_id: [])

    assert dingtalk.validate(CONFIG)["valid"] is True
