"""The annotator's live suites, run against a deployed rask lakehouse with the annotator installed beside it.

They drive the lakehouse through its public doors with this directory's own harness (`lakehouse_doors`), put on
`sys.path` here because the test modules import it by its flat name. Offline runs deselect them by their `e2e` marker.
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
