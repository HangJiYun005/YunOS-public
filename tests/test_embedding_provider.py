from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

from memory.embedding_provider import LocalSentenceTransformerProvider, get_embedding_provider


class _FakeSentenceTransformer:
    instances: list["_FakeSentenceTransformer"] = []

    def __init__(self, model_name: str, cache_folder: str, device: str, **kwargs) -> None:
        self.model_name = model_name
        self.cache_folder = cache_folder
        self.device = device
        self.kwargs = kwargs
        self.encode_calls: list[tuple[object, dict]] = []
        self.__class__.instances.append(self)

    def encode(self, texts, **kwargs):
        self.encode_calls.append((texts, kwargs))
        vector = [3.0, 4.0] + [0.0] * 510
        if isinstance(texts, str):
            return vector
        return [vector for _ in texts]


class LocalSentenceTransformerProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSentenceTransformer.instances.clear()

    def test_local_provider_is_lazy_normalized_and_reuses_the_model(self):
        fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
        with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
            provider = get_embedding_provider("local_sentence_transformer")

            self.assertEqual("BAAI/bge-small-zh-v1.5", provider.embedding_model)
            self.assertEqual([], _FakeSentenceTransformer.instances)

            first = provider.embed_text("YunOS memory")
            second = provider.embed_text("YunOS memory")

        self.assertEqual(1, len(_FakeSentenceTransformer.instances))
        self.assertEqual(512, len(first))
        self.assertAlmostEqual(1.0, sum(value * value for value in first))
        self.assertEqual(first, second)
        self.assertEqual("E:/AI/Models/sentence-transformers", _FakeSentenceTransformer.instances[0].cache_folder)
        self.assertTrue(_FakeSentenceTransformer.instances[0].kwargs["local_files_only"])

    def test_local_provider_honors_process_environment_configuration(self):
        fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
        environment = {
            "YUNOS_EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
            "YUNOS_MODEL_CACHE_DIR": "E:/AI/Models/sentence-transformers",
            "YUNOS_EMBEDDING_DEVICE": "cpu",
        }
        with patch.dict(os.environ, environment, clear=False), patch.dict(
            sys.modules, {"sentence_transformers": fake_module}
        ):
            provider = get_embedding_provider("local_sentence_transformer")
            provider.embed_text("configured model")

        instance = _FakeSentenceTransformer.instances[0]
        self.assertEqual(environment["YUNOS_EMBEDDING_MODEL"], instance.model_name)
        self.assertEqual(environment["YUNOS_MODEL_CACHE_DIR"], instance.cache_folder)
        self.assertEqual(environment["YUNOS_EMBEDDING_DEVICE"], instance.device)

    def test_local_provider_returns_zero_vector_for_empty_text_without_loading_model(self):
        fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
        with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
            provider = get_embedding_provider("local_sentence_transformer")
            embedding = provider.embed_text("   ")

        self.assertEqual(512, len(embedding))
        self.assertTrue(all(value == 0.0 for value in embedding))
        self.assertEqual([], _FakeSentenceTransformer.instances)

    def test_local_provider_reports_missing_optional_dependency_without_text(self):
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            provider = get_embedding_provider("local_sentence_transformer")
            with self.assertRaisesRegex(RuntimeError, "sentence-transformers"):
                provider.embed_text("dependency check")

    def test_local_provider_never_opens_network_connections(self):
        fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
        with patch.dict(sys.modules, {"sentence_transformers": fake_module}), patch(
            "socket.create_connection", side_effect=AssertionError("network not allowed")
        ):
            embedding = get_embedding_provider("local_sentence_transformer").embed_text("local only")

        self.assertEqual(512, len(embedding))

    def test_local_identity_is_complete_and_fingerprint_is_cached(self):
        provider = LocalSentenceTransformerProvider()
        snapshot = "E:/AI/Models/sentence-transformers/models--BAAI--bge-small-zh-v1.5/snapshots/revision-a"
        with patch.object(provider, "_local_snapshot_path", return_value=__import__("pathlib").Path(snapshot)), patch(
            "memory.embedding_provider._model_manifest_fingerprint", return_value="c" * 64
        ) as fingerprint:
            first = provider.get_embedding_identity()
            second = provider.get_embedding_identity()

        self.assertEqual(first, second)
        self.assertEqual("BAAI/bge-small-zh-v1.5", first["model_name"])
        self.assertEqual("revision-a", first["model_revision"])
        self.assertEqual("c" * 64, first["model_fingerprint"])
        self.assertTrue(first["normalized"])
        self.assertEqual(1, fingerprint.call_count)

    def test_local_fingerprint_is_not_derived_from_model_slug_or_dimension(self):
        LocalSentenceTransformerProvider._identity_cache.clear()
        first_provider = LocalSentenceTransformerProvider()
        second_provider = LocalSentenceTransformerProvider()
        with patch.object(first_provider, "_local_snapshot_path", return_value=__import__("pathlib").Path("E:/models/snapshots/a")), patch.object(
            second_provider, "_local_snapshot_path", return_value=__import__("pathlib").Path("E:/models/snapshots/b")
        ), patch("memory.embedding_provider._model_manifest_fingerprint", side_effect=["a" * 64, "b" * 64]):
            first = first_provider.get_embedding_identity()
            second = second_provider.get_embedding_identity()

        self.assertEqual(first["model_name"], second["model_name"])
        self.assertEqual(first["embedding_dim"], second["embedding_dim"])
        self.assertNotEqual(first["model_fingerprint"], second["model_fingerprint"])


if __name__ == "__main__":
    unittest.main()
