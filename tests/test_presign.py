from __future__ import annotations

import urllib.parse

from app.services import presign


def test_presigned_url_round_trip(monkeypatch) -> None:
    monkeypatch.setenv("SYSTEM_SIGNING_KEY", "s" * 32)
    url = presign.sign_file_url("https://weknora.example.test/", "local://7/a.png", 7, 60)
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/api/v1/files/presigned"
    assert query["file_path"] == ["local://7/a.png"]
    assert query["tenant_id"] == ["7"]
    assert presign.verify_file_url(
        query["file_path"][0], 7, query["expires"][0], query["sig"][0]
    )
    assert not presign.verify_file_url(
        query["file_path"][0], 8, query["expires"][0], query["sig"][0]
    )
    assert not presign.verify_file_url(
        query["file_path"][0], 7, query["expires"][0], "deadbeef"
    )


def test_presign_requires_a_valid_system_key(monkeypatch) -> None:
    monkeypatch.delenv("SYSTEM_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SYSTEM_AES_KEY", raising=False)
    assert not presign.verify_file_url("local://7/a.png", 7, "9999999999", "abc")
    try:
        presign.sign_file_url("https://weknora.example.test", "local://7/a.png", 7)
    except ValueError:
        pass
    else:
        raise AssertionError("signing without a key must fail")

    monkeypatch.setenv("SYSTEM_AES_KEY", "weknora-system-aes-key-32bytes!!")
    assert not presign.verify_file_url("local://7/a.png", 7, "9999999999", "abc")
