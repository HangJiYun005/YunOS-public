from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "hybrid_retrieval_cases.json"
RRF_K = 60


def load_cases(path: str | Path = DEFAULT_FIXTURE) -> list[dict]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Hybrid evaluation fixture must contain a cases list")
    return cases


def evaluate_cases(cases: list[dict]) -> dict:
    evaluated = []
    keyword_hits = semantic_hits = hybrid_hits = 0
    keyword_nonempty = semantic_nonempty = hybrid_nonempty = mixed_sources = 0
    natural_keyword_nonempty = natural_count = short_keyword_nonempty = short_count = 0

    for case in cases:
        keyword = _top3(case.get("keyword_results", []))
        semantic = _top3(case.get("semantic_results", []))
        hybrid = _rrf_top3(keyword, semantic)
        expected = set(case["expected_source_ids"])
        keyword_hits += _has_expected(keyword, expected)
        semantic_hits += _has_expected(semantic, expected)
        hybrid_hits += _has_expected(hybrid, expected)
        keyword_nonempty += bool(keyword)
        semantic_nonempty += bool(semantic)
        hybrid_nonempty += bool(hybrid)
        mixed_sources += {item["source_kind"] for item in hybrid} == {
            "keyword_message", "semantic_summary"
        }
        if case["query_type"] == "natural":
            natural_count += 1
            natural_keyword_nonempty += bool(keyword)
        elif case["query_type"] == "short":
            short_count += 1
            short_keyword_nonempty += bool(keyword)
        else:
            raise ValueError("query_type must be natural or short")
        evaluated.append(
            {
                "case_id": case["id"],
                "query_type": case["query_type"],
                "expected_source_ids": case["expected_source_ids"],
                "results": {
                    "keyword_top3": _result_ids(keyword),
                    "semantic_top3": _result_ids(semantic),
                    "hybrid_top3": _result_ids(hybrid),
                },
            }
        )

    case_count = len(cases)
    keyword_rate = _rate(keyword_hits, case_count)
    semantic_rate = _rate(semantic_hits, case_count)
    hybrid_rate = _rate(hybrid_hits, case_count)
    natural_rate = _rate(natural_keyword_nonempty, natural_count)
    short_rate = _rate(short_keyword_nonempty, short_count)
    return {
        "case_count": case_count,
        "keyword_hit_at_3": keyword_rate,
        "semantic_hit_at_3": semantic_rate,
        "hybrid_hit_at_3": hybrid_rate,
        "keyword_nonempty_rate": _rate(keyword_nonempty, case_count),
        "semantic_nonempty_rate": _rate(semantic_nonempty, case_count),
        "hybrid_nonempty_rate": _rate(hybrid_nonempty, case_count),
        "mixed_source_rate": _rate(mixed_sources, case_count),
        "natural_query_keyword_nonempty_rate": natural_rate,
        "short_query_keyword_nonempty_rate": short_rate,
        "keyword_query_gap": short_rate > natural_rate + 0.25,
        "quality_gate": "passed" if hybrid_rate >= max(keyword_rate, semantic_rate) else "failed",
        "cases": evaluated,
    }


def _top3(results: list[dict]) -> list[dict]:
    return sorted(results, key=lambda item: (int(item["rank"]), item["source_id"]))[:3]


def _rrf_top3(keyword: list[dict], semantic: list[dict]) -> list[dict]:
    merged = [dict(item, rrf_score=1.0 / (RRF_K + int(item["rank"]))) for item in [*keyword, *semantic]]
    return sorted(merged, key=lambda item: (-item["rrf_score"], item["source_kind"], item["source_id"]))[:3]


def _result_ids(results: list[dict]) -> list[dict]:
    return [{"source_id": item["source_id"], "source_kind": item["source_kind"]} for item in results]


def _has_expected(results: list[dict], expected: set[str]) -> int:
    return int(any(item["source_id"] in expected for item in results))


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate synthetic hybrid retrieval cases.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = evaluate_cases(load_cases(args.fixture))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False if args.json else True, indent=2))
    return 0 if result["quality_gate"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
