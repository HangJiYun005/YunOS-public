from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path


DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_MODEL_CACHE_DIR = "E:/AI/Models/sentence-transformers"
DEFAULT_EMBEDDING_DEVICE = "cpu"
LOCAL_EMBEDDING_DIM = 512
DETERMINISTIC_PROTOCOL = "deterministic_hash_normalized_v1"
LOCAL_EMBEDDING_PROTOCOL = "sentence_transformers_normalized_v1"


class BaseEmbeddingProvider:
    provider_name = "base"
    embedding_model = "base"
    dim = 0

    def embed_text(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]

    def get_embedding_identity(self) -> dict:
        raise NotImplementedError


class DeterministicTestEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "deterministic_test"
    embedding_model = "deterministic_test"

    def __init__(self, dim: int = 32) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed_text(self, text: str) -> list[float]:
        if not text.strip():
            return [0.0] * self.dim

        vector = [0.0] * self.dim
        tokens = re.findall(r"\w+", text.casefold())
        if not tokens:
            tokens = [text.casefold()]

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + (digest[5] / 255.0)
            vector[index] += sign * weight

        return normalize_vector(vector)

    def get_embedding_identity(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "model_name": self.embedding_model,
            "model_revision": "deterministic_v1",
            "model_fingerprint": hashlib.sha256(
                f"yunos:{self.embedding_model}:{self.dim}:{DETERMINISTIC_PROTOCOL}".encode("ascii")
            ).hexdigest(),
            "embedding_protocol": DETERMINISTIC_PROTOCOL,
            "embedding_dim": self.dim,
            "normalized": True,
        }


class LocalSentenceTransformerProvider(BaseEmbeddingProvider):
    """Local-only BGE embedding provider with a single lazily loaded model."""

    provider_name = "local_sentence_transformer"
    _identity_cache: dict[str, dict] = {}

    def __init__(
        self,
        model_name: str | None = None,
        cache_dir: str | None = None,
        device: str | None = None,
    ) -> None:
        self.embedding_model = model_name or os.environ.get(
            "YUNOS_EMBEDDING_MODEL", DEFAULT_LOCAL_EMBEDDING_MODEL
        )
        self.cache_dir = cache_dir or os.environ.get(
            "YUNOS_MODEL_CACHE_DIR", DEFAULT_MODEL_CACHE_DIR
        )
        self.device = device or os.environ.get("YUNOS_EMBEDDING_DEVICE", DEFAULT_EMBEDDING_DEVICE)
        self.dim = LOCAL_EMBEDDING_DIM
        self._model = None
        self._identity: dict | None = None

        if self.embedding_model != DEFAULT_LOCAL_EMBEDDING_MODEL:
            raise ValueError("Only BAAI/bge-small-zh-v1.5 is allowed for local embeddings")
        if _normalized_path(self.cache_dir) != _normalized_path(DEFAULT_MODEL_CACHE_DIR):
            raise ValueError("Local embedding cache must be E:/AI/Models/sentence-transformers")

    @property
    def cache_root(self) -> str:
        return DEFAULT_MODEL_CACHE_DIR

    def embed_text(self, text: str) -> list[float]:
        if not text.strip():
            return [0.0] * self.dim
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors = [[0.0] * self.dim for _ in texts]
        nonempty_positions = [index for index, text in enumerate(texts) if text.strip()]
        if not nonempty_positions:
            return vectors

        model = self._load_model()
        encoded = model.encode(
            [texts[index] for index in nonempty_positions],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        for index, vector in zip(nonempty_positions, encoded):
            normalized = normalize_vector([float(value) for value in vector])
            if len(normalized) != self.dim:
                raise RuntimeError("Local embedding model returned an unexpected vector dimension")
            vectors[index] = normalized
        return vectors

    def get_embedding_identity(self) -> dict:
        if self._identity is not None:
            return dict(self._identity)

        snapshot = self._local_snapshot_path()
        cache_key = str(snapshot).casefold()
        cached = self._identity_cache.get(cache_key)
        if cached is None:
            cached = {
                "provider_name": self.provider_name,
                "model_name": self.embedding_model,
                "model_revision": snapshot.name,
                "model_fingerprint": _model_manifest_fingerprint(snapshot),
                "embedding_protocol": LOCAL_EMBEDDING_PROTOCOL,
                "embedding_dim": self.dim,
                "normalized": True,
            }
            self._identity_cache[cache_key] = cached
        self._identity = dict(cached)
        return dict(self._identity)

    def _local_snapshot_path(self) -> Path:
        repository_dir = Path(self.cache_root) / "models--BAAI--bge-small-zh-v1.5" / "snapshots"
        if not repository_dir.is_dir():
            raise RuntimeError("Local embedding model identity is unavailable")
        snapshots = sorted(path for path in repository_dir.iterdir() if path.is_dir())
        if len(snapshots) != 1:
            raise RuntimeError("Local embedding model identity is unavailable")
        return snapshots[0]

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Local embedding provider requires sentence-transformers; install it before use"
            ) from exc
        try:
            self._model = SentenceTransformer(
                self.embedding_model,
                cache_folder=self.cache_dir,
                device=self.device,
                local_files_only=True,
            )
        except Exception as exc:
            raise RuntimeError("Unable to load the configured local embedding model") from exc
        return self._model


def normalize_vector(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in v))
    if norm == 0.0:
        return [0.0 for _ in v]
    return [float(value / norm) for value in v]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0

    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    dot = sum(left * right for left, right in zip(a, b))
    return float(dot / (norm_a * norm_b))


def get_embedding_provider(name: str = "deterministic_test") -> BaseEmbeddingProvider:
    if name == "deterministic_test":
        return DeterministicTestEmbeddingProvider()
    if name == "local_sentence_transformer":
        return LocalSentenceTransformerProvider()
    raise ValueError(f"Unsupported embedding provider: {name}")


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def _model_manifest_fingerprint(snapshot: Path) -> str:
    files = sorted(path for path in snapshot.rglob("*") if path.is_file())
    if not files:
        raise RuntimeError("Local embedding model identity is unavailable")
    manifest = []
    for path in files:
        manifest.append(
            {
                "path": path.relative_to(snapshot).as_posix(),
                "sha256": _file_sha256(path),
                "size": path.stat().st_size,
            }
        )
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
