from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.embedding_provider import cosine_similarity, get_embedding_provider


DEFAULT_FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "embedding_quality_cases.json"


def load_cases(path: str | Path = DEFAULT_FIXTURE_PATH) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        cases = json.load(handle)
    if not isinstance(cases.get("candidates"), list) or not isinstance(cases.get("queries"), list):
        raise ValueError("Embedding quality fixture must contain candidates and queries lists")
    return cases


def evaluate_provider(provider_name: str, fixture_path: str | Path = DEFAULT_FIXTURE_PATH) -> dict:
    cases = load_cases(fixture_path)
    candidates = cases["candidates"]
    queries = cases["queries"]
    provider = get_embedding_provider(provider_name)
    candidate_vectors = provider.embed_texts([candidate["text"] for candidate in candidates])
    query_vectors = provider.embed_texts([query["text"] for query in queries])
    if len(candidate_vectors) != len(candidates) or len(query_vectors) != len(queries):
        raise RuntimeError("Embedding provider returned an unexpected batch size")

    results = []
    top1_hits = 0
    top3_hits = 0
    reciprocal_rank_total = 0.0
    for query, query_vector in zip(queries, query_vectors):
        expected_ids = list(query["expected_source_ids"])
        scored = [
            {
                "source_id": candidate["id"],
                "score": cosine_similarity(query_vector, candidate_vector),
            }
            for candidate, candidate_vector in zip(candidates, candidate_vectors)
        ]
        scored.sort(key=lambda item: item["score"], reverse=True)
        ranked_ids = [item["source_id"] for item in scored]
        if ranked_ids[0] in expected_ids:
            top1_hits += 1
        if any(source_id in expected_ids for source_id in ranked_ids[:3]):
            top3_hits += 1
        first_rank = next(
            (index for index, source_id in enumerate(ranked_ids, start=1) if source_id in expected_ids),
            None,
        )
        if first_rank is not None:
            reciprocal_rank_total += 1.0 / first_rank
        results.append(
            {
                "query_id": query["id"],
                "expected_source_ids": expected_ids,
                "top3": scored[:3],
            }
        )

    case_count = len(queries)
    return {
        "provider": provider_name,
        "model_name": provider.embedding_model,
        "embedding_dim": provider.dim,
        "case_count": case_count,
        "candidate_count": len(candidates),
        "top1_accuracy": top1_hits / case_count if case_count else 0.0,
        "hit_rate_at_3": top3_hits / case_count if case_count else 0.0,
        "mean_reciprocal_rank": reciprocal_rank_total / case_count if case_count else 0.0,
        "queries": results,
    }


def compare_providers(provider_names: list[str], fixture_path: str | Path = DEFAULT_FIXTURE_PATH) -> dict:
    evaluations = [evaluate_provider(provider_name, fixture_path) for provider_name in provider_names]
    by_provider = {result["provider"]: result for result in evaluations}
    local = by_provider.get("local_sentence_transformer")
    deterministic = by_provider.get("deterministic_test")
    quality_gate = "not_applicable"
    if local and deterministic:
        quality_gate = "passed" if (
            local["top1_accuracy"] >= 0.70
            and local["hit_rate_at_3"] >= 0.85
            and local["mean_reciprocal_rank"] > deterministic["mean_reciprocal_rank"]
        ) else "failed"
    return {"evaluations": evaluations, "quality_gate": quality_gate}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate local embedding quality using a synthetic fixture.")
    provider_group = parser.add_mutually_exclusive_group(required=True)
    provider_group.add_argument("--provider")
    provider_group.add_argument("--compare", nargs="+", metavar="PROVIDER")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE_PATH))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.provider:
            payload = evaluate_provider(args.provider, args.fixture)
            payload["quality_gate"] = "not_compared"
        else:
            payload = compare_providers(args.compare, args.fixture)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("quality_gate") != "failed" else 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
