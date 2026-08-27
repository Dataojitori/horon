"""Embedding utility — calls OpenRouter voyage-4-large for 1024-dim vectors."""
from __future__ import annotations

import json
import logging
import math
import os
import struct
import urllib.error
import urllib.request

_logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_MODEL = "voyageai/voyage-4-large"
EMBEDDING_DIMENSIONS = 1024


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


def _get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. "
            "Add it to .env or set the environment variable.")
    return key


def get_embedding(text: str) -> list[float] | None:
    """Compute a 1024-dim embedding for text via OpenRouter voyage-4-large.

    Returns None (with a warning log) on API failure so that mutations
    don't crash if the network is unavailable.
    """
    if not text or not text.strip():
        return None

    try:
        key = _get_api_key()
    except RuntimeError:
        _logger.warning("Skipping embedding: OPENROUTER_API_KEY not configured.")
        return None

    payload = json.dumps({
        "model": EMBEDDING_MODEL,
        "input": [text],
        "dimensions": EMBEDDING_DIMENSIONS,
    }).encode("utf-8")

    req = urllib.request.Request(
        _OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        embedding = body["data"][0]["embedding"]
        
        if not isinstance(embedding, list):
            _logger.warning("Embedding is not a list")
            return None
            
        if len(embedding) != EMBEDDING_DIMENSIONS:
            _logger.warning(
                "Unexpected embedding dimension: got %d, expected %d",
                len(embedding), EMBEDDING_DIMENSIONS)
            return None
        
        # calculate norm, will throw TypeError if elements are not numbers
        norm = math.sqrt(sum(x * x for x in embedding))
        if norm > 0:
            embedding = [float(x) / norm for x in embedding]
            
        return embedding
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, 
            json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as e:
        _logger.warning("Embedding API call failed: %s (%s)", type(e).__name__, e)
        return None


def sync_single_embedding(db_instance, concept_id: int, text: str) -> bool:
    """Synchronously fetch embedding and update concept_embeddings table.
    
    Returns True if embedding was fetched and written to database, False otherwise.
    Designed to be called via `_post_commit_hooks` AFTER the main transaction 
    has committed, so it doesn't hold up the database lock.
    Only writes/updates embedding if the current disclosure in database still equals `text`.
    """
    emb = get_embedding(text)
    if not emb:
        return False
        
    emb_blob = embedding_to_blob(emb)
    
    try:
        from ._db_common import _now
        now = _now()
        # We are outside the main transaction lock now, so a quick new transaction is safe.
        with db_instance.conn:
            cursor = db_instance.conn.execute(
                """
                INSERT INTO concept_embeddings (concept_id, embedding, embedding_model, updated_at)
                SELECT id, ?, ?, ?
                FROM concepts
                WHERE id = ? AND disclosure = ?
                ON CONFLICT(concept_id) DO UPDATE SET
                    embedding=excluded.embedding,
                    embedding_model=excluded.embedding_model,
                    updated_at=excluded.updated_at
                """,
                (emb_blob, EMBEDDING_MODEL, now, concept_id, text)
            )
            return cursor.rowcount > 0
    except Exception as e:
        _logger.warning("Failed to write embedding to database: %s", e)
        return False
