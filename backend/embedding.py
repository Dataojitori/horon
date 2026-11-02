"""书腰的语义向量：调嵌入模型取向量、向量和 SQLite blob 互转、写进 concept_embeddings。
用哪个嵌入模型、怎么发请求、记账，都在 backend/llm.py。"""
from __future__ import annotations

import math
import struct

from .llm import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, EMBEDDING_PROVIDER_ORDER, EMBEDDINGS_ENDPOINT, openrouter_post


def embedding_to_blob(emb: list[float] | None) -> bytes | None:
    """Serialize a float vector to a little-endian binary blob for SQLite storage."""
    if not emb:
        return None
    return struct.pack(f'<{len(emb)}f', *emb)


def blob_to_embedding(blob: bytes | None) -> list[float] | None:
    """Deserialize a little-endian binary blob from SQLite into a list of floats."""
    if not blob:
        return None
    count = len(blob) // 4
    return list(struct.unpack(f'<{count}f', blob))


def embed_texts(texts: list[str], timeout: int = 120) -> list[list[float]]:
    """Embed several texts in one OpenRouter request.

    Input: non-empty texts; timeout in seconds for the whole request.
    Output: one unit-length EMBEDDING_DIMENSIONS-dim vector per text, in input order.
    Raises if the API key is missing, the request fails, or the response is malformed.
    """
    data = openrouter_post(EMBEDDINGS_ENDPOINT, {
        "model": EMBEDDING_MODEL,
        "input": texts,
        "dimensions": EMBEDDING_DIMENSIONS,
        "provider": {"order": EMBEDDING_PROVIDER_ORDER},
    }, timeout)["data"]
    vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    if len(vectors) != len(texts):
        raise ValueError(f"Got {len(vectors)} embeddings for {len(texts)} texts")

    normalised = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"Unexpected embedding shape (expected {EMBEDDING_DIMENSIONS} floats)")
        norm = math.sqrt(sum(x * x for x in vector))
        normalised.append([float(x) / norm for x in vector])
    return normalised


def sync_embeddings(db_instance, items: list[tuple[int, str]], timeout: int = 30) -> int:
    """Fetch the embeddings of several when_to_read texts in one request and store them.

    Input: the HoronDB and (concept id, the when_to_read text it had when queued) pairs.
    Called AFTER the main transaction has committed, so it doesn't hold up the database
    lock; failures (no key, network, API error) are raised to the caller, which logs them
    without undoing the mutation.
    Output: how many embeddings were written. A concept whose when_to_read no longer equals
    its queued text (it was changed again meanwhile) is skipped.
    """
    from ._db_common import _now
    if not items:
        return 0
    vectors = embed_texts([text for _, text in items], timeout=timeout)
    written = 0
    # We are outside the main transaction lock now, so a quick new transaction is safe.
    with db_instance.conn:
        for (concept_id, text), vector in zip(items, vectors):
            cursor = db_instance.conn.execute(
                """
                INSERT INTO concept_embeddings (concept_id, embedding, embedding_model, updated_at)
                SELECT id, ?, ?, ?
                FROM concepts
                WHERE id = ? AND when_to_read = ?
                ON CONFLICT(concept_id) DO UPDATE SET
                    embedding=excluded.embedding,
                    embedding_model=excluded.embedding_model,
                    updated_at=excluded.updated_at
                """,
                (embedding_to_blob(vector), EMBEDDING_MODEL, _now(), concept_id, text)
            )
            written += cursor.rowcount
    return written


def sync_single_embedding(db_instance, concept_id: int, text: str) -> bool:
    """sync_embeddings for one node. Output: True if written, False if its when_to_read changed meanwhile."""
    return sync_embeddings(db_instance, [(concept_id, text)]) > 0
