"""Opt-in integration coverage for an S3-compatible MinIO endpoint."""

from __future__ import annotations

import os
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("WEKNORA_OBJECT_STORAGE_INTEGRATION") != "1",
    reason="set WEKNORA_OBJECT_STORAGE_INTEGRATION=1 to exercise MinIO",
)


def test_real_minio_put_get_copy_and_delete(monkeypatch) -> None:
    boto3 = pytest.importorskip("boto3")
    botocore_config = pytest.importorskip("botocore.config")

    from app.services import object_storage

    endpoint = os.environ.get("WEKNORA_MINIO_ENDPOINT", "http://127.0.0.1:9000")
    access_key = os.environ.get("WEKNORA_MINIO_ACCESS_KEY", "minioadmin")
    secret_key = os.environ.get("WEKNORA_MINIO_SECRET_KEY", "minioadmin")
    bucket = f"pyweknora-it-{uuid.uuid4().hex[:12]}"
    config = {
        "endpoint": endpoint,
        "region": "us-east-1",
        "bucket_name": bucket,
        "access_key_id": access_key,
        "secret_access_key": secret_key,
    }
    monkeypatch.setenv("SSRF_WHITELIST_EXTRA", "127.0.0.1")
    monkeypatch.setattr(
        object_storage,
        "resolve_write_target",
        lambda tenant_id, backend_id="", provider="": (
            "backend-minio",
            "minio",
            config,
            bucket,
            "us-east-1",
        ),
    )
    monkeypatch.setattr(
        object_storage,
        "resolve_config",
        lambda reference, tenant_id: (
            "minio",
            config,
            bucket,
            object_storage._split_reference(reference)[4],
            "us-east-1",
        ),
    )
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=botocore_config.Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )
    client.create_bucket(Bucket=bucket)
    references: list[str] = []
    try:
        source = object_storage.save_bytes(
            b"real minio payload",
            7,
            "Report.PDF",
            provider="minio",
            knowledge_id="knowledge-source",
            content_type="application/pdf",
        )
        references.append(source)
        assert object_storage.read_object(source, 7) == b"real minio payload"

        copied = object_storage.copy_object(
            source,
            7,
            "knowledge-copy",
            filename="Clone.PDF",
        )
        references.append(copied)
        assert object_storage.read_object(copied, 7) == b"real minio payload"

        object_storage.delete_object(source, 7)
        object_storage.delete_object(source, 7)
        with pytest.raises(object_storage.ObjectStorageError) as missing:
            object_storage.read_object(source, 7)
        assert missing.value.status == 404
    finally:
        for reference in references:
            try:
                object_storage.delete_object(reference, 7)
            except Exception:
                pass
        response = client.list_objects_v2(Bucket=bucket)
        keys = [item["Key"] for item in response.get("Contents", [])]
        if keys:
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in keys]},
            )
        client.delete_bucket(Bucket=bucket)
