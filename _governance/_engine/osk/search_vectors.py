"""Local, rebuildable search vectors for the same documents as the lexical index."""
from __future__ import annotations

from contextlib import closing
import hashlib
import http.client
import json
import logging
import sqlite3

import numpy as np

from .vault import Vault

MODEL = "bge-m3:latest"
BATCH = 24
DEPTH = 40
WIDTH = 1024
LOG = logging.getLogger(__name__)


class EmbeddingError(ValueError):
    """The configured local embedding model or its response cannot be used."""


def _request(path, timeout, payload=None):
    # A fixed loopback connection ignores HTTP proxies and never follows redirects.
    try:
        with closing(http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout)) as conn:
            body = json.dumps(payload).encode() if payload is not None else None
            conn.request("POST" if body is not None else "GET", path, body, {"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise EmbeddingError(f"local Ollama {path} returned HTTP {response.status}")
            result = json.load(response)
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeError) as exc:
        raise EmbeddingError(f"local Ollama {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise EmbeddingError("invalid local Ollama response")
    return result


def _model_digest(timeout):
    models = _request("/api/tags", timeout).get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("name") == MODEL and isinstance(item.get("digest"), str) and item["digest"]:
                return item["digest"]
    raise EmbeddingError(f"install {MODEL} in local Ollama to use the search index")


def _embed(texts, timeout):
    result = _request("/api/embed", timeout, {"model": MODEL, "input": texts})
    try:
        vectors = np.asarray(result.get("embeddings"), dtype="<f4")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingError("invalid embedding values") from exc
    if vectors.shape != (len(texts), WIDTH) or not np.isfinite(vectors).all():
        raise EmbeddingError("invalid embedding shape or values")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or not (norms > 0).all():
        raise EmbeddingError("invalid embedding norm")
    return (vectors / norms).astype("<f4", copy=False)


def combine(lexical, dense, k):
    """Keep lexical leaders, fuse two ranked lists for the first eight, then resume lexical order."""
    scores = {}
    for ranking in (lexical[:DEPTH], dense[:DEPTH]):
        for rank, title in enumerate(ranking, 1):
            scores[title] = scores.get(title, 0.0) + 1 / (60 + rank)
    fused = sorted(scores, key=lambda title: (-scores[title], title))
    first = list(dict.fromkeys([*lexical[:3], *fused]))[:min(k, 8)]
    return (first + [title for title in lexical if title not in first])[:k]


def _cached(db, digest):
    vectors = {}
    for key, blob in db.execute("SELECT key, vector FROM vectors WHERE model=?", (digest,)):
        if isinstance(blob, bytes) and len(blob) == WIDTH * 4:
            vector = np.frombuffer(blob, dtype="<f4")
            if np.isfinite(vector).all() and np.isclose(np.dot(vector, vector), 1, atol=1e-4):
                vectors[key] = vector
    return vectors


class VectorIndex:
    def __init__(self, vault: Vault, paths):
        self.path = vault.root / ".osk" / "search-vectors.sqlite3"
        self.paths = paths
        self.snapshot = None

    def _rows(self, db, digest, full):
        docs = []
        for title, _, node in sorted(self.paths, key=lambda item: item[0]):
            preview = str(node.meta.get("summary") or "").strip() or node.body.strip()
            text = title + "\n" + preview[:500]
            docs.append((title, hashlib.sha256(text.encode()).digest(), text))
        cached = _cached(db, digest)
        missing = [(key, text) for _, key, text in docs if key not in cached]
        added = []
        # Bulk imports can be indexed explicitly without holding up an interactive query.
        if full or len(missing) <= BATCH:
            for i in range(0, len(missing), BATCH):
                batch = missing[i:i + BATCH]
                vectors = _embed([text for _, text in batch], 120 if full else 2)
                for (key, _), vector in zip(batch, vectors):
                    cached[key] = vector
                    added.append((digest, key, vector.tobytes()))
        current = [(title, key) for title, key, _ in docs if key in cached]
        titles = [title for title, _ in current]
        matrix = np.stack([cached[key] for _, key in current]) if current else np.empty((0, WIDTH), dtype="<f4")
        return titles, matrix, added, [key for _, key in current]

    def _refresh(self, db, digest, full):
        titles, matrix, added, keys = self._rows(db, digest, full)
        if (full or added) and _model_digest(2) != digest:
            raise EmbeddingError("local embedding model changed during indexing")
        with db:
            if added:
                db.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?)", added)
            if full:
                db.execute("CREATE TEMP TABLE live(key BLOB PRIMARY KEY)")
                db.executemany("INSERT INTO live VALUES(?)", ((key,) for key in keys))
                db.execute("DELETE FROM vectors WHERE model != ? OR key NOT IN (SELECT key FROM live)", (digest,))
                db.execute("INSERT OR REPLACE INTO active VALUES(1,?)", (digest,))
        return titles, matrix, len(added)

    def prepare(self):
        digest = _model_digest(5)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                db.execute("CREATE TABLE IF NOT EXISTS vectors(model TEXT, key BLOB, vector BLOB, PRIMARY KEY(model,key))")
                db.execute("CREATE TABLE IF NOT EXISTS active(id INTEGER PRIMARY KEY, model TEXT)")
                titles, _, embedded = self._refresh(db, digest, True)
        except (OSError, sqlite3.Error) as exc:
            raise EmbeddingError(f"cannot prepare {self.path}: {exc}") from exc
        self.snapshot = None
        return {"model": MODEL, "digest": digest, "documents": len(titles), "embedded": embedded}

    def nearest(self, query):
        try:
            if not self.path.is_file():
                return []
            # mode=rw prevents a reader from recreating an index removed between stat and open.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.25)) as db:
                active = db.execute("SELECT model FROM active WHERE id=1").fetchone()
                if not active:
                    return []
                digest = _model_digest(2)
                if digest != active[0]:
                    raise EmbeddingError("local embedding model changed; run search-index")
                stat = self.path.stat()
                signature = (digest, stat.st_ino, stat.st_mtime_ns, stat.st_size)
                snapshot = self.snapshot
                if snapshot is None or snapshot[0] != signature:
                    titles, matrix, _ = self._refresh(db, digest, False)
                    snapshot = self.snapshot = (signature, titles, matrix)
                _, titles, matrix = snapshot
                if not titles:
                    return []
                vector = _embed([query], 2)[0]
                if _model_digest(2) != digest:
                    raise EmbeddingError("local embedding model changed during search")
                order = np.argsort(-np.einsum("ij,j->i", matrix, vector), kind="stable")[:DEPTH]
                return [titles[i] for i in order]
        except (OSError, sqlite3.Error, EmbeddingError) as exc:
            LOG.warning("Semantic search unavailable; using lexical results (%s)", exc)
            return []
