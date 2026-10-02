"""OpenRouter calls: voyage-4-large embeddings (1024-dim) and the Jev decisions endpoint."""
from __future__ import annotations

import json
import math
import os
import struct
import urllib.error
import urllib.request

_OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_MODEL = "voyageai/voyage-4-large"
EMBEDDING_DIMENSIONS = 1024
JEV_MODEL = "typesafe/jev-1.13"
DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"


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


def openrouter_post(url: str, payload: dict, timeout: int) -> dict:
    """POST a JSON payload to an OpenRouter endpoint and return the decoded JSON reply.

    Input: the endpoint URL, the request payload, and a timeout in seconds. The API key is
    read from OPENROUTER_API_KEY (surrounding whitespace removed).
    Raises RuntimeError when the key is unset or blank, and when OpenRouter answers with an
    HTTP error; that message carries the status and OpenRouter's response body, which says
    why (bad key, no credit, unknown model, payload too large, ...).
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. "
            "Add it to .env or set the environment variable.")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code} from {url}: {body}") from e


def embed_texts(texts: list[str], timeout: int = 120) -> list[list[float]]:
    """Embed several texts in one OpenRouter request.

    Input: non-empty texts; timeout in seconds for the whole request.
    Output: one unit-length EMBEDDING_DIMENSIONS-dim vector per text, in input order.
    Raises if the API key is missing, the request fails, or the response is malformed.
    """
    data = openrouter_post(_OPENROUTER_URL, {
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
    """Fetch the embedding of a disclosure and store it in concept_embeddings.

    Input: the HoronDB, the concept id, and the disclosure text it had when the hook was queued.
    Designed to be called via `_post_commit_hooks` AFTER the main transaction has
    committed, so it doesn't hold up the database lock; failures (no key, network, API
    error) are raised to the caller, which logs them without undoing the mutation.
    Output: True if the embedding was written; False if the concept's disclosure no longer
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
            WHERE id = ? AND disclosure = ?
            ON CONFLICT(concept_id) DO UPDATE SET
                embedding=excluded.embedding,
                embedding_model=excluded.embedding_model,
                updated_at=excluded.updated_at
            """,
            (emb_blob, EMBEDDING_MODEL, _now(), concept_id, text)
        )
        return cursor.rowcount > 0
