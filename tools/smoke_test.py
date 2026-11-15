from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "README.md",
    ".gitignore",
    "tools/privacy_scan.py",
    "tools/search_memory.py",
    "tools/memory_browser.py",
    "tools/local_web.py",
    "tools/create_test_db.py",
    "tools/audit_project.py",
    "tools/memory_package_preview.py",
    "tools/evidence_selection_preview.py",
    "tools/temporal_memory_candidates_preview.py",
    "tools/context_assembly_preview.py",
    "tools/context_delivery_preview.py",
    "tools/runtime_thin_slice.py",
    "tools/build_profile_authorization_manifest.py",
    "tools/profile_authorization_snapshot.py",
    "tools/init_runtime_sidecar.py",
    "config/projects_public.json",
    "web/index.html",
    "web/app.js",
    "web/style.css",
    "public_demo/product_brief.md",
    "public_demo/mock_memory_items.json",
    "public_demo/brand_kit_public.md",
    "docs/memory_browser_mvp.md",
    "docs/local_web_ui.md",
    "docs/timeline_view.md",
    "docs/source_platforms.md",
    "docs/source_trace_search.md",
    "docs/test_database_policy.md",
    "docs/safe_git_workflow.md",
    "docs/memory_package_contract_v7_0.md",
    "docs/evidence_selection_v7_1.md",
    "docs/temporal_memory_candidates_v7_2.md",
    "docs/context_assembly_v7_3.md",
    "docs/transparent_context_delivery_v7_4.md",
    "docs/approved_profile_control_v8_0_a_lite.md",
    "docs/deepseek_runtime_thin_slice_v8_0_bc.md",
    "docs/production_authorization_fidelity_recovery_v8_0_d2.md",
    "docs/fixed_production_authority_boundary_v8_0_d2_1.md",
    "docs/deterministic_sqlite_snapshot_v8_0_d2_2.md",
    "docs/production_delivery_integration_v8_0_e.md",
    "docs/production_runtime_execution_ui_v8_0_g.md",
    "docs/manifests/v8_0_d2_r2_recovery_source.json",
    "docs/manifests/v8_0_d2_r1_recovery_source.json",
    "memory/memory_package.py",
    "memory/evidence_selection.py",
    "memory/temporal_memory_candidates.py",
    "memory/context_assembly.py",
    "memory/context_delivery.py",
    "memory/production_delivery.py",
    "memory/approved_profile_control.py",
    "memory/deepseek_adapter.py",
    "memory/runtime_thin_slice.py",
    "memory/production_runtime_execution.py",
    "memory/profile_authorization_fidelity.py",
    "memory/sqlite_snapshot.py",
]


def main() -> int:
    duplicates = sorted({rel_path for rel_path in REQUIRED_FILES if REQUIRED_FILES.count(rel_path) > 1})
    missing = [
        rel_path for rel_path in REQUIRED_FILES if not (PROJECT_ROOT / rel_path).is_file()
    ]

    if duplicates or missing:
        print("AYUNOS SMOKE TEST WARNING")
        if duplicates:
            print("Duplicate checks:")
            for rel_path in duplicates:
                print(f"- {rel_path}")
        if missing:
            print("Missing files:")
            for rel_path in missing:
                print(f"- {rel_path}")
        return 1

    print("AYUNOS SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
