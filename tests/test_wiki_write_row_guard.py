from app.services.wiki_write_row_guard import (
    INGEST_ROW_IDENTITY_LOSS_LIMIT,
    dropped_rows_message,
    missing_row_identities,
    row_identity_loss_ratio,
    table_row_identities,
    unique_row_identity_count,
)


def ledger(*holders: str) -> str:
    body = "# Certificate ledger\n\n| Holder | Certificate |\n| --- | --- |\n"
    return body + "".join(f"| {holder} | CERT-2020 |\n" for holder in holders)


def test_missing_row_identities_compares_subjects_not_formatting() -> None:
    full = ledger("张三", "李四", "王五")
    assert missing_row_identities(full, full) == []
    assert missing_row_identities(full, ledger("王五", "张三", "李四")) == []
    assert missing_row_identities(full, ledger("**张三**", " 李四 ", "王五")) == []
    assert missing_row_identities(full, ledger("张三", "李四", "王五", "赵六")) == []
    assert missing_row_identities("just prose", "shorter prose") == []
    assert missing_row_identities(full, ledger("张三")) == ["李四", "王五"]


def test_missing_row_identities_uses_a_set_and_reports_each_loss_once() -> None:
    existing = ledger("张三", "张三")
    assert missing_row_identities(existing, ledger("张三")) == []
    assert missing_row_identities(existing, ledger()) == ["张三"]


def test_unique_row_identity_count_counts_duplicates_once() -> None:
    assert unique_row_identity_count("just prose") == 0
    assert unique_row_identity_count(ledger("张三", "张三")) == 1
    assert unique_row_identity_count(ledger("张三", "张三", "李四", "王五", "李四")) == 3


def test_row_identity_loss_ratio_has_an_exact_twenty_percent_boundary() -> None:
    assert row_identity_loss_ratio(2, 0) == 0
    assert row_identity_loss_ratio(1, 5) <= INGEST_ROW_IDENTITY_LOSS_LIMIT
    assert row_identity_loss_ratio(1, 4) > INGEST_ROW_IDENTITY_LOSS_LIMIT


def test_table_row_identities_skip_headers_and_cover_every_table() -> None:
    content = (
        "| 姓名 | 证书编号 |\n| --- | --- |\n| 张三 | CERT-1 |\n\nprose\n\n"
        "| 项目 | 金额 |\n| --- | --- |\n| 差旅 | 100 |\n| 会务 | 200 |\n"
    )
    assert table_row_identities(content) == ["张三", "差旅", "会务"]
    rewritten = content.replace("| 会务 | 200 |\n", "")
    assert missing_row_identities(content, rewritten) == ["会务"]


def test_table_row_identities_use_the_next_non_empty_cell() -> None:
    assert table_row_identities("| | 张三 | CERT-1 |") == ["张三"]
    assert table_row_identities("a | b") == []
    assert table_row_identities("| lone pipe") == []


def test_table_row_identities_handle_crlf_documents() -> None:
    content = "| Header | Value |\r\n| --- | --- |\r\n| 张三 | CERT-1 |\r\n"
    assert table_row_identities(content) == ["张三"]


def test_dropped_rows_message_applies_agent_and_ingest_limits() -> None:
    existing = ledger("持证人01", "持证人02", "持证人03", "持证人04", "持证人05")
    at_limit = ledger("持证人01", "持证人02", "持证人03", "持证人04")
    over_limit = ledger("持证人01", "持证人02", "持证人03")

    assert dropped_rows_message(existing, at_limit, "entity/pmp", loss_limit=0) != ""
    assert dropped_rows_message(
        existing, at_limit, "entity/pmp", loss_limit=INGEST_ROW_IDENTITY_LOSS_LIMIT
    ) == ""
    refused = dropped_rows_message(
        existing, over_limit, "entity/pmp", loss_limit=INGEST_ROW_IDENTITY_LOSS_LIMIT
    )
    assert "lost 2 of 5 table row identities" in refused
    assert "stored page is unchanged" in refused
