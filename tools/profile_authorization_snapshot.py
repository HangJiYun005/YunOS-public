from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.sqlite_snapshot import (
    snapshot_production_profile_authorization_database,
    snapshot_summary,
)


def main() -> int:
    """Print a privacy-safe snapshot summary for the fixed production profile DB."""
    snapshot = snapshot_production_profile_authorization_database()
    print(json.dumps(snapshot_summary(snapshot), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
