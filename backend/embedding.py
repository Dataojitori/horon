"""书腰的语义向量：调嵌入模型取向量、向量和 SQLite blob 互转、写进 concept_embeddings。
用哪个嵌入模型、怎么发请求、记账，都在 backend/llm.py。"""
from __future__ import annotations

import math
import struct

from .llm import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, EMBEDDINGS_ENDPOINT, openrouter_post


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


def sync_single_embedding(db_instance, concept_id: int, text: str) -> bool:
    """Fetch the embedding of a when_to_read and store it in concept_embeddings.

    Input: the HoronDB, the concept id, and the when_to_read text it had when the hook was queued.
    Designed to be called via `_post_commit_hooks` AFTER the main transaction has
    committed, so it doesn't hold up the database lock; failures (no key, network, API
    error) are raised to the caller, which logs them without undoing the mutation.
    Output: True if the embedding was written; False if the concept's when_to_read no longer
    equals `text` (it was changed again meanwhile), in which case nothing is written.
    """
    from ._db_common import _now
    emb_blob = embedding_to_blob(embed_texts([text], timeout=30)[0])
    # We are outside the main transaction lock now, so a quick new transaction is safe.
    with db_instance.conn:
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
            (emb_blob, EMBEDDING_MODEL, _now(), concept_id, text)
        )
        return cursor.rowcount > 0
