"""The chart's demo Job fills its volume once per dataset: a run writes each demo dataset the volume lacks and leaves
every one it holds as it is.

The Job (`chart/templates/demo.yaml`) runs `scripts/seed_demo_corpus.py --once <volume>` on every release that changes
its spec, so a reinstall or an upgrade from git must converge on the data already there: add a dataset the seeder
learned since, and never rewrite tables a person's annotations name. Driven as the Job runs it: the seeder as a process
over one directory, every file under it read before and after the run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import lance_namespace
from lance_namespace import DropNamespaceRequest, DropTableRequest


ROOT = Path(__file__).resolve().parents[2]


def _seed_once(volume: Path) -> subprocess.CompletedProcess[str]:
    return _started(volume).communicate_result()


class _Run(subprocess.Popen[str]):
    def communicate_result(self) -> subprocess.CompletedProcess[str]:
        out, err = self.communicate(timeout=300)
        return subprocess.CompletedProcess(self.args, self.returncode, out, err)


def _started(volume: Path) -> _Run:
    """The seeder as the Job runs it, started and not waited for."""
    return _Run([sys.executable, str(ROOT / "scripts/seed_demo_corpus.py"), "--once", str(volume)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _files(volume: Path) -> dict[str, tuple[int, int]]:
    return {str(path.relative_to(volume)): (path.stat().st_size, path.stat().st_mtime_ns) for path in sorted(volume.rglob("*")) if path.is_file()}


def test_a_second_seed_over_a_filled_volume_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    volume = tmp_path / "demo"
    first = _seed_once(volume)
    filled = _files(volume)

    second = _seed_once(volume)

    assert first.returncode == 0, first.stderr
    assert {name.split("/")[0] for name in filled} == {".seeded.d", ".seeding.lock", "archive", "lakehouse", "loose"}
    assert (second.returncode, second.stdout.strip()) == (0, f"{volume} holds every demo dataset (.seeded.d): nothing written")
    assert _files(volume) == filled


def test_a_volume_an_earlier_seeder_filled_gets_the_datasets_it_lacks_and_keeps_the_rest(tmp_path: Path) -> None:
    """A volume an earlier seeder filled at some commit: one `.seeded` file, and only the datasets that seeder knew. Here
    it wrote neither the letters nor the films (whatever it knew, the run reads what the volume holds, never a list). A
    first conversion is cut short part way through marking and leaves the old file; the next writes those two, keeps
    every other file byte for byte, leaves a marker per dataset and removes the old file last."""
    volume = tmp_path / "demo"
    assert _seed_once(volume).returncode == 0
    shutil.rmtree(volume / ".seeded.d")
    (volume / ".seeded").write_text("seeded by scripts/seed_demo_corpus.py\n", encoding="utf-8")
    lakehouse = lance_namespace.connect("dir", {"root": str(volume / "lakehouse")})
    for namespace, tables in (("letters", ["paragraphs"]), ("films", ["films", "shots"])):
        for table in tables:
            lakehouse.drop_table(DropTableRequest(id=["demo", namespace, table]))
        lakehouse.drop_namespace(DropNamespaceRequest(id=["demo", namespace]))
    # The directory catalog's own manifest records the tables the run adds: every other file is a dataset's, and is kept.
    kept = {name: stat for name, stat in _files(volume).items() if not name.startswith((".seeded", "lakehouse/__manifest/", ".seeding"))}

    # A conversion cut short: the corpus's new marker cannot be written (a directory stands in its place), so the run
    # stops part way through marking; the earlier seeder's file must still be there for the next run to convert from.
    (volume / ".seeded.d" / "lakehouse-corpus").mkdir(parents=True)
    cut_short = _seed_once(volume)
    still_marked = (volume / ".seeded").is_file()
    (volume / ".seeded.d" / "lakehouse-corpus").rmdir()

    run = _seed_once(volume)

    after = _files(volume)
    assert (cut_short.returncode != 0, still_marked) == (True, True), cut_short.stdout
    assert run.returncode == 0, run.stderr
    assert [line for line in run.stdout.splitlines() if line.startswith("seeded")] == ["seeded lakehouse demo$films", "seeded lakehouse demo$letters"]
    assert {name: stat for name, stat in after.items() if name in kept} == kept
    assert not (volume / ".seeded").exists(), "the earlier seeder's file outlived the conversion"
    assert sorted(path.name for path in (volume / ".seeded.d").iterdir()) == [
        "archive",
        "lakehouse-corpus",
        "lakehouse-films",
        "lakehouse-letters",
        "lakehouse-recordings",
        "loose",
    ]


def test_two_seeds_at_once_write_each_dataset_once(tmp_path: Path) -> None:
    """Two Jobs over one empty volume (a reinstall while the first still runs): the second waits for the first's lock and
    finds every dataset marked, so each is written once and the volume reads as one seed left it."""
    volume = tmp_path / "demo"
    first, second = _started(volume), _started(volume)

    runs = [first.communicate_result(), second.communicate_result()]

    assert [r.returncode for r in runs] == [0, 0], [r.stderr for r in runs]
    written = sorted(line for r in runs for line in r.stdout.splitlines() if line.startswith("seeded"))
    assert len(written) == 6 and len(set(written)) == 6, written
    assert sorted(path.name for path in (volume / ".seeded.d").iterdir()) == [
        "archive",
        "lakehouse-corpus",
        "lakehouse-films",
        "lakehouse-letters",
        "lakehouse-recordings",
        "loose",
    ]
