"""Record what the viewer answers for the set-ups the app's browser suite draws on the demo's plain tables.

Writes `frontend/app/e2e/seeded-answers.json` (`tests.derived_recording`), which the suite's mock serves. Run it after
a change to the viewer's derived doors or to the demo's plain tables; the viewer's suite fails until the recording is
what the viewer answers.

The viewer is built in this process with authentication off and never listens on a port: the recording is its answers
to in-process requests (`fastapi.testclient`), as the viewer's own suite reads them.

usage: uv run python -m scripts.record_derived_answers
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def main() -> None:
    # Read when the viewer is imported and built; the recording's own sources are declared in `record`.
    os.environ.setdefault("WORKBENCH_SOURCES", '[{"kind": "tables", "name": "boot", "uri": "/nonexistent.lance"}]')
    os.environ.setdefault("WORKBENCH_OIDC_ENABLED", "false")
    os.environ.setdefault("WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED", "true")
    from tests.derived_recording import RECORDING, record, write

    with tempfile.TemporaryDirectory() as root:
        answers = record(Path(root))
    write(answers)
    print(f"recorded {len(answers)} answers to {RECORDING}")


if __name__ == "__main__":
    main()
