"""The chart's demo Job fills its volume once: a second run over a filled volume writes nothing and exits 0.

The Job (`chart/templates/demo.yaml`) runs `scripts/seed_demo_corpus.py --once <volume>` on every release that changes
its spec, so a reinstall from git must converge on the data already there rather than rewrite tables a person's
annotations name. Driven as the Job runs it: the seeder as a process, twice, over one directory, every file under it
read before and after the second run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _seed_once(volume: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/seed_demo_corpus.py"), "--once", str(volume)], capture_output=True, text=True, timeout=300, check=False
    )


def _files(volume: Path) -> dict[str, tuple[int, int]]:
    return {str(path.relative_to(volume)): (path.stat().st_size, path.stat().st_mtime_ns) for path in sorted(volume.rglob("*")) if path.is_file()}


def test_a_second_seed_over_a_filled_volume_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    volume = tmp_path / "demo"
    first = _seed_once(volume)
    filled = _files(volume)

    second = _seed_once(volume)

    assert first.returncode == 0, first.stderr
    assert {name.split("/")[0] for name in filled} == {".seeded", "archive", "lakehouse", "loose"}
    assert (second.returncode, second.stdout.strip()) == (0, f"{volume} holds a completed seed (.seeded): nothing written")
    assert _files(volume) == filled
