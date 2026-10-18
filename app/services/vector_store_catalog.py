"""Metadata for vector-store engines exposed by the registration UI."""

from __future__ import annotations

import os
import re
from typing import Any


REGISTERABLE_ENGINE_TYPES = frozenset(
    {
        "elasticsearch",
        "qdrant",
        "milvus",
        "tencent_vectordb",
        "weaviate",
        "doris",
        "opensearch",
    }
)
_SAFE_INDEX_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,127}$")
_INDEX_NUMBER_BOUNDS = {
    "number_of_shards": 64,
    "number_of_replicas": 10,
    "shard_number": 64,
    "replication_factor": 10,
    "shards_num": 64,
    "replica_number": 10,
    "desired_shard_count": 64,
    "buckets_num": 64,
    "replication_num": 10,
}


def _tencent_replica_number() -> int:
    raw = os.getenv("TENCENT_VECTORDB_REPLICA_NUMBER", "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return value if value >= 0 else 1


def supported_vector_store_types() -> list[dict[str, Any]]:
    return [
        {
            "type": "elasticsearch",
            "display_name": "Elasticsearch",
            "connection_fields": [
                {"name": "addr", "type": "string", "required": True, "default": "http://localhost:9200", "description": "URL"},
                {"name": "username", "type": "string", "required": False, "default": "elastic", "description": "Username"},
                {"name": "password", "type": "string", "required": False, "sensitive": True, "description": "Password"},
            ],
            "index_fields": [
                {"name": "index_name", "type": "string", "required": False, "default": "weknora", "description": "Index Name"},
                {"name": "number_of_shards", "type": "number", "required": False, "default": 4, "description": "Shards"},
                {"name": "number_of_replicas", "type": "number", "required": False, "default": 1, "description": "Replicas"},
            ],
        },
        {
            "type": "qdrant",
            "display_name": "Qdrant",
            "connection_fields": [
                {"name": "host", "type": "string", "required": True, "default": "localhost", "description": "Host"},
                {"name": "port", "type": "number", "required": False, "default": 6334, "description": "Port"},
                {"name": "api_key", "type": "string", "required": False, "sensitive": True, "description": "API Key"},
                {"name": "use_tls", "type": "boolean", "required": False, "default": False, "description": "Use TLS"},
            ],
            "index_fields": [
                {"name": "collection_prefix", "type": "string", "required": False, "default": "weknora_embeddings", "description": "Collection Prefix"},
                {"name": "shard_number", "type": "number", "required": False, "default": 1, "description": "Shard Number"},
                {"name": "replication_factor", "type": "number", "required": False, "default": 1, "description": "Replication Factor"},
            ],
        },
        {
            "type": "milvus",
            "display_name": "Milvus",
            "connection_fields": [
                {"name": "addr", "type": "string", "required": True, "default": "localhost:19530", "description": "Address"},
                {"name": "database", "type": "string", "required": False, "description": "Database Name"},
                {"name": "username", "type": "string", "required": False, "default": "root", "description": "Username"},
                {"name": "password", "type": "string", "required": False, "sensitive": True, "description": "Password"},
            ],
            "index_fields": [
                {"name": "collection_name", "type": "string", "required": False, "default": "weknora_embeddings", "description": "Collection Name"},
                {"name": "shards_num", "type": "number", "required": False, "default": 1, "description": "Shards (write parallelism)"},
                {"name": "replica_number", "type": "number", "required": False, "default": 1, "description": "In-memory Replicas (read HA)"},
            ],
        },
        {
            "type": "tencent_vectordb",
            "display_name": "Tencent VectorDB",
            "connection_fields": [
                {"name": "addr", "type": "string", "required": True, "default": "http://localhost:8080", "description": "Address"},
                {"name": "username", "type": "string", "required": True, "description": "Username"},
                {"name": "api_key", "type": "string", "required": True, "sensitive": True, "description": "API Key"},
                {"name": "database", "type": "string", "required": False, "default": "weknora", "description": "Database"},
            ],
            "index_fields": [
                {"name": "collection_name", "type": "string", "required": False, "default": "weknora_embeddings", "description": "Collection Name"},
                {"name": "shards_num", "type": "number", "required": False, "default": 1, "description": "Shards"},
                {"name": "replica_number", "type": "number", "required": False, "default": _tencent_replica_number(), "description": "Replicas"},
            ],
        },
        {
            "type": "weaviate",
            "display_name": "Weaviate",
            "connection_fields": [
                {"name": "host", "type": "string", "required": True, "default": "weaviate:8080", "description": "Host"},
                {"name": "grpc_address", "type": "string", "required": False, "default": "weaviate:50051", "description": "gRPC Address"},
                {"name": "scheme", "type": "string", "required": False, "default": "http", "description": "Scheme"},
                {"name": "api_key", "type": "string", "required": False, "sensitive": True, "description": "API Key"},
            ],
            "index_fields": [
                {"name": "collection_prefix", "type": "string", "required": False, "default": "Weknora_embeddings", "description": "Collection Prefix"},
                {"name": "desired_shard_count", "type": "number", "required": False, "default": 1, "description": "Shard Count"},
                {"name": "replication_factor", "type": "number", "required": False, "default": 1, "description": "Replication Factor"},
            ],
        },
        {
            "type": "doris",
            "display_name": "Apache Doris",
            "connection_fields": [
                {"name": "addr", "type": "string", "required": True, "default": "doris-fe:9030", "description": "FE MySQL Address (host:port)"},
                {"name": "http_port", "type": "number", "required": False, "default": 8030, "description": "FE HTTP Port (Stream Load)"},
                {"name": "database", "type": "string", "required": True, "default": "weknora", "description": "Database"},
                {"name": "username", "type": "string", "required": False, "default": "root", "description": "Username"},
                {"name": "password", "type": "string", "required": False, "sensitive": True, "description": "Password"},
            ],
            "index_fields": [
                {"name": "collection_prefix", "type": "string", "required": False, "default": "weknora_embeddings", "description": "Table Prefix"},
                {"name": "buckets_num", "type": "number", "required": False, "default": 10, "description": "Buckets per table"},
                {"name": "replication_num", "type": "number", "required": False, "default": 1, "description": "Replication Num"},
            ],
        },
        {
            "type": "opensearch",
            "display_name": "OpenSearch",
            "connection_fields": [
                {"name": "addr", "type": "string", "required": True, "default": "https://localhost:9200", "description": "URL"},
                {"name": "username", "type": "string", "required": False, "default": "admin", "description": "Username"},
                {"name": "password", "type": "string", "required": False, "sensitive": True, "description": "Password"},
                {
                    "name": "insecure_skip_verify",
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Skip TLS certificate verification. For self-signed dev clusters only — never enable in production.",
                },
            ],
            "index_fields": [
                {"name": "index_name", "type": "string", "required": False, "default": "weknora", "description": "Index Name"},
                {"name": "number_of_shards", "type": "number", "required": False, "default": 4, "min": 1, "max": 64, "description": "Shards"},
                {"name": "number_of_replicas", "type": "number", "required": False, "default": 1, "min": 0, "max": 10, "description": "Replicas"},
                {
                    "name": "hnsw_m",
                    "type": "number",
                    "required": False,
                    "default": 16,
                    "min": 2,
                    "max": 100,
                    "immutable": True,
                    "description": "HNSW graph degree (M). Immutable after index creation.",
                },
                {
                    "name": "hnsw_ef_construction",
                    "type": "number",
                    "required": False,
                    "default": 100,
                    "min": 2,
                    "max": 4096,
                    "immutable": True,
                    "description": "HNSW build candidate list. Higher (e.g. 200-512) improves recall at the cost of build time. Immutable after creation.",
                },
                {
                    "name": "hnsw_ef_search",
                    "type": "number",
                    "required": False,
                    "default": 100,
                    "min": 1,
                    "max": 10000,
                    "immutable": True,
                    "description": "HNSW search candidate list. Effective on the faiss engine; the lucene engine reads it at query time. Immutable (no settings-update path).",
                },
                {
                    "name": "knn_engine",
                    "type": "string",
                    "required": False,
                    "default": "lucene",
                    "enum": ["lucene", "faiss"],
                    "immutable": True,
                    "description": "k-NN backend.",
                },
            ],
        },
    ]


def is_environment_store_id(value: str) -> bool:
    return str(value or "").startswith("__env_")


def is_registerable_engine_type(value: str) -> bool:
    return str(value or "").strip().lower() in REGISTERABLE_ENGINE_TYPES


def validate_connection_config(engine_type: str, connection: dict[str, Any]) -> str | None:
    engine = str(engine_type or "").strip().lower()
    required = {
        "elasticsearch": ("addr",),
        "qdrant": ("host",),
        "milvus": ("addr",),
        "tencent_vectordb": ("addr", "username", "api_key"),
        "weaviate": ("host",),
        "doris": ("addr", "database"),
        "opensearch": ("addr",),
    }.get(engine)
    if required is None:
        return f"unsupported engine type: {engine}"
    for field in required:
        if not str(connection.get(field) or "").strip():
            return f"{field} is required for {engine}"
    return None


def validate_index_config(engine_type: str, index: dict[str, Any]) -> str | None:
    for field in ("index_name", "collection_prefix", "collection_name"):
        value = str(index.get(field) or "")
        if value and not _SAFE_INDEX_NAME.fullmatch(value):
            return (
                f"{field} must start with a letter and contain only alphanumeric, "
                "underscore, or hyphen characters (max 128)"
            )
    for field, maximum in _INDEX_NUMBER_BOUNDS.items():
        value = index.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{field} must be between 0 and {maximum}"
        if value < 0 or value > maximum or int(value) != value:
            return f"{field} must be between 0 and {maximum}"
    if str(engine_type or "").strip().lower() == "opensearch":
        bounds = {
            "hnsw_m": (2, 100),
            "hnsw_ef_construction": (2, 4096),
            "hnsw_ef_search": (1, 10000),
        }
        for field, (minimum, maximum) in bounds.items():
            value = index.get(field)
            if value in (None, "", 0):
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < minimum
                or value > maximum
                or int(value) != value
            ):
                return f"{field} must be between {minimum} and {maximum}"
        knn_engine = str(index.get("knn_engine") or "")
        if knn_engine and knn_engine not in {"lucene", "faiss"}:
            return 'knn_engine must be "lucene" or "faiss"'
    return None


def vector_store_addresses(engine_type: str, connection: dict[str, Any]) -> list[str]:
    engine = str(engine_type or "").strip().lower()
    if engine in {
        "elasticsearch",
        "opensearch",
        "milvus",
        "tencent_vectordb",
        "doris",
    }:
        return [str(connection.get("addr") or "")]
    if engine == "qdrant":
        host = str(connection.get("host") or "")
        port = connection.get("port")
        return [f"{host}:{port}" if host and port else host]
    if engine == "weaviate":
        return [
            str(connection.get("host") or ""),
            str(connection.get("grpc_address") or ""),
        ]
    return []


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in {"", None, False, 0}}


def _environment_store(driver: str) -> dict[str, Any] | None:
    get = os.getenv
    if driver == "postgres":
        store_id, name, engine = "__env_postgres__", "PostgreSQL", "postgres"
        connection, index = {"use_default_connection": True}, {}
    elif driver == "sqlite":
        store_id, name, engine = "__env_sqlite__", "SQLite", "sqlite"
        connection, index = {}, {}
    elif driver in {"elasticsearch_v7", "elasticsearch_v8"}:
        version = driver.rsplit("_", 1)[-1]
        store_id = f"__env_{driver}__"
        name = f"Elasticsearch v{version}"
        engine = "elasticsearch"
        connection = _compact(
            {
                "addr": get("ELASTICSEARCH_ADDR", ""),
                "username": get("ELASTICSEARCH_USERNAME", ""),
                "password": get("ELASTICSEARCH_PASSWORD", ""),
            }
        )
        index = _compact({"index_name": get("ELASTICSEARCH_INDEX", "")})
    elif driver == "opensearch":
        store_id, name, engine = "__env_opensearch__", "OpenSearch", "opensearch"
        connection = _compact(
            {
                "addr": get("OPENSEARCH_ADDR", ""),
                "username": get("OPENSEARCH_USERNAME", ""),
                "password": get("OPENSEARCH_PASSWORD", ""),
                "insecure_skip_verify": get("OPENSEARCH_INSECURE_SKIP_VERIFY", "").strip().lower()
                == "true",
            }
        )
        index = _compact({"index_name": get("OPENSEARCH_INDEX", "")})
    elif driver == "qdrant":
        store_id, name, engine = "__env_qdrant__", "Qdrant", "qdrant"
        try:
            port = int(get("QDRANT_PORT", "6334"))
        except ValueError:
            port = 6334
        tls = get("QDRANT_USE_TLS", "").strip().lower()
        connection = _compact(
            {
                "host": get("QDRANT_HOST", "") or "localhost",
                "port": port,
                "api_key": get("QDRANT_API_KEY", ""),
                "use_tls": bool(tls and tls not in {"false", "0"}),
            }
        )
        index = _compact({"collection_prefix": get("QDRANT_COLLECTION", "")})
    elif driver == "milvus":
        store_id, name, engine = "__env_milvus__", "Milvus", "milvus"
        connection = _compact(
            {
                "addr": get("MILVUS_ADDRESS", ""),
                "username": get("MILVUS_USERNAME", ""),
                "password": get("MILVUS_PASSWORD", ""),
            }
        )
        index = {}
    elif driver == "tencent_vectordb":
        store_id, name, engine = (
            "__env_tencent_vectordb__",
            "Tencent VectorDB",
            "tencent_vectordb",
        )
        connection = _compact(
            {
                "addr": get("TENCENT_VECTORDB_ADDR", ""),
                "username": get("TENCENT_VECTORDB_USERNAME", ""),
                "api_key": get("TENCENT_VECTORDB_API_KEY", ""),
                "database": get("TENCENT_VECTORDB_DATABASE", ""),
            }
        )
        index = _compact({"collection_name": get("TENCENT_VECTORDB_COLLECTION", "")})
    elif driver == "weaviate":
        store_id, name, engine = "__env_weaviate__", "Weaviate", "weaviate"
        connection = _compact(
            {
                "host": get("WEAVIATE_HOST", ""),
                "grpc_address": get("WEAVIATE_GRPC_ADDRESS", ""),
                "scheme": get("WEAVIATE_SCHEME", ""),
                "api_key": get("WEAVIATE_API_KEY", ""),
            }
        )
        index = {}
    elif driver == "doris":
        store_id, name, engine = "__env_doris__", "Apache Doris", "doris"
        raw_port = get("DORIS_HTTP_PORT", "")
        try:
            http_port = int(raw_port) if raw_port else 0
        except ValueError:
            http_port = 0
        connection = _compact(
            {
                "addr": get("DORIS_ADDR", ""),
                "http_port": http_port,
                "database": get("DORIS_DATABASE", ""),
                "username": get("DORIS_USERNAME", ""),
                "password": get("DORIS_PASSWORD", ""),
            }
        )
        index = _compact({"collection_prefix": get("DORIS_TABLE_PREFIX", "")})
    else:
        return None
    return {
        "id": store_id,
        "name": name,
        "engine_type": engine,
        "connection_config": connection,
        "index_config": index,
        "source": "env",
        "readonly": True,
    }


def environment_vector_stores() -> list[dict[str, Any]]:
    stores: list[dict[str, Any]] = []
    for driver in str(os.getenv("RETRIEVE_DRIVER") or "").split(","):
        store = _environment_store(driver.strip())
        if store is not None:
            stores.append(store)
    return stores


def find_environment_vector_store(store_id: str) -> dict[str, Any] | None:
    return next(
        (store for store in environment_vector_stores() if store["id"] == store_id),
        None,
    )


def vector_store_endpoint(engine_type: str, connection: dict[str, Any]) -> str:
    addr = str(connection.get("addr") or "")
    if addr:
        database = str(connection.get("database") or "")
        return f"{addr}/{database}" if database else addr
    host = str(connection.get("host") or "")
    if host:
        try:
            port = int(connection.get("port") or 6334)
        except (TypeError, ValueError):
            port = 6334
        return f"{host}:{port}"
    if connection.get("use_default_connection"):
        return "__default_postgres__"
    return ""


def vector_store_index_name(engine_type: str, index: dict[str, Any]) -> str:
    if engine_type == "elasticsearch":
        return str(index.get("index_name") or "xwrag_default")
    if engine_type == "opensearch":
        return str(index.get("index_name") or "")
    if engine_type == "qdrant":
        return str(index.get("collection_prefix") or "weknora_embeddings")
    if engine_type in {"milvus", "tencent_vectordb"}:
        return str(index.get("collection_name") or "weknora_embeddings")
    if engine_type == "weaviate":
        return str(index.get("collection_prefix") or "Weknora_embeddings")
    if engine_type == "doris":
        return str(
            index.get("collection_prefix")
            or index.get("collection_name")
            or "weknora_embeddings"
        )
    return ""
