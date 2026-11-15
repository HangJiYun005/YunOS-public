from __future__ import annotations

from pathlib import Path
import argparse
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SCAN_TARGETS = [
    PROJECT_ROOT / "docs",
    PROJECT_ROOT / "tools",
    PROJECT_ROOT / "public_demo",
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "README.md",
]

STRICT_EXTRA_TARGETS = [
    PROJECT_ROOT / ".gitignore",
    PROJECT_ROOT / "docs" / "safe_git_workflow.md",
]

EXCLUDED_DIRS = {
    "data",
    "imports",
    "backups",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".git",
    "dist",
    "build",
    "__pycache__",
    "docs_private",
    "config_private",
}

DEFAULT_EXCLUDED_FILES = {
    PROJECT_ROOT / "tools" / "privacy_scan.py",
    PROJECT_ROOT / "docs" / "safe_git_workflow.md",
}

KEYWORDS = [
    "ExampleUser",
    "ExampleUser",
    "ExampleUser",
    "ExampleUser",
    "ExampleUser",
    "ExampleUser",
    r"C:\Users\ExampleUser",
    r"E:\AyunOS\data",
    "messages.jsonl",
    "ayunos_chatgpt.db",
    "conversations_index.csv",
    "assets_index.csv",
    "ayunos_chatgpt_import_pack.zip",
    "\u79c1\u4eba\u804a\u5929",
    "\u5b8c\u6574\u804a\u5929\u8bb0\u5f55",
    "\u4eb2\u5b50\u5173\u7cfb",
    "\u604b\u7231",
    "\u4f53\u91cd",
    "\u8102\u80aa\u809d",
    "\u751f\u6d3b\u8d39",
]

TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".ps1",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

MAX_SNIPPET_CHARS = 160

DOCS_ALLOWLIST = {
    (PROJECT_ROOT / "README.md", "data/ayunos_chatgpt.db"),
}


def is_excluded(path: Path) -> bool:
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return True
    return any(part in EXCLUDED_DIRS for part in relative.parts)


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {".gitignore"}


def iter_scan_files(strict: bool = False):
    targets = list(DEFAULT_SCAN_TARGETS)
    if strict:
        targets.extend(STRICT_EXTRA_TARGETS)

    excluded_files = set() if strict else DEFAULT_EXCLUDED_FILES

    for target in targets:
        if not target.exists():
            continue
        if target.is_file():
            if target not in excluded_files and not is_excluded(target) and is_text_file(target):
                yield target
            continue
        for path in target.rglob("*"):
            if path.is_dir() or path in excluded_files or is_excluded(path) or not is_text_file(path):
                continue
            yield path


def clip_line(line: str) -> str:
    compact = " ".join(line.strip().split())
    if len(compact) <= MAX_SNIPPET_CHARS:
        return compact
    return compact[: MAX_SNIPPET_CHARS - 3] + "..."


def scan_file(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            return
    except OSError as exc:
        rel = path.relative_to(PROJECT_ROOT)
        yield rel, 0, "[read-error]", f"Could not read file: {exc}"
        return

    lowered_keywords = [(keyword, keyword.lower()) for keyword in KEYWORDS]
    for line_number, line in enumerate(lines, start=1):
        lowered_line = line.lower()
        if any(path == allowed_path and allowed_text in line for allowed_path, allowed_text in DOCS_ALLOWLIST):
            continue
        matched = [
            keyword for keyword, lowered in lowered_keywords if lowered in lowered_line
        ]
        if matched:
            rel = path.relative_to(PROJECT_ROOT)
            yield rel, line_number, ", ".join(matched), clip_line(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan committable AyunOS files for privacy risks.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also scan privacy_scan.py, .gitignore, and docs/safe_git_workflow.md",
    )
    args = parser.parse_args()

    findings = []
    for path in iter_scan_files(strict=args.strict):
        findings.extend(scan_file(path))

    if not findings:
        print("PRIVACY SCAN PASSED")
        return 0

    print("PRIVACY SCAN WARNING")
    for rel, line_number, keywords, snippet in findings:
        print(f"{rel}:{line_number}: {keywords}: {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
