#!/usr/bin/env python3
"""CLI adapter for the engine's registered-deliverable validator."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "task-runner" / "scripts"))
from deliverables import check_deliverables, main

if __name__ == "__main__":
    raise SystemExit(main())
