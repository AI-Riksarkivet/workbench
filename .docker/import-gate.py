"""Import every module of the packages an image serves, at BUILD time.

Run as `python /tmp/import-gate.py <pkg> [<pkg> ...]` from a dockerfile, against the venv the runtime stage ships. A
non-zero exit fails the build.

**Why this exists.** The suite runs in the workspace venv, which holds every member's dependencies, so a module whose
import needs a package its own member does not declare passes every test and still fails in an image whose venv is
`uv sync --package <pkg>` and nothing else: the pod crash-loops on `ModuleNotFoundError`. This is the first place
where the deployable's real dependency closure is the only one present.

**It reports every failure, not the first**, so one build lists every missing dependency.

**Reading that report.** Dagger does not print a failing exec's stdout (a blocked build says only
`withExec … exit code: 1`), and a failing RUN leaves no image to look into. Reproduce the image's closure on the host
instead:

    UV_PROJECT_ENVIRONMENT=/tmp/gatevenv uv sync --frozen --no-dev --package <pkg>
    /tmp/gatevenv/bin/python .docker/import-gate.py <pkg>

(For an image that syncs several packages, pass every `--package` its dockerfile does.) Every dockerfile names the
venv's interpreter by its absolute path, since the gate runs before `PATH` names the venv.

A FILE rather than an inline `RUN python -c`: a heredoc RUN needs a `# syntax=` directive none of these dockerfiles
declare, and the one-liner form must be escaped through `sh -c` twice.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys


def main(argv: list[str]) -> int:
    packages = argv
    if not packages:
        print("import-gate: no packages given", file=sys.stderr)
        return 2

    failures: list[str] = []
    executed: list[str] = []

    modules: list[str] = []
    for package in packages:
        # Enumerating the submodules needs the package itself imported, and THAT can fail — which is
        # in fact the common case, because a package's `__init__` usually pulls in the app. Uncaught,
        # it ended the run with a raw traceback and no report at all: the build still failed, but the
        # message a build log showed was a stack trace instead of the one line naming the module and
        # the missing dependency. Classified here exactly as below, then skipped.
        try:
            found = importlib.import_module(package)
        except Exception as exc:
            line = f"{package}: {type(exc).__name__}: {exc}"
            (failures if _is_packaging_failure(exc) else executed).append(line)
            continue
        # Appended only AFTER it imported — listing it up front made a failing root package appear in
        # both the failure list and the retry loop, and the report read "2/1 modules".
        modules.append(package)
        # walk_packages over __path__ rather than a hand-listed set: a module added later is covered
        # without anyone remembering to add it here, which is the only way a gate stays honest.
        modules += [m.name for m in pkgutil.walk_packages(found.__path__, package + ".")]

    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            line = f"{name}: {type(exc).__name__}: {exc}"
            (failures if _is_packaging_failure(exc) else executed).append(line)

    # A module that raised something OTHER than an import error got far enough to RUN, which means
    # every import in its chain resolved — the only thing this gate is contracted to prove. Reported,
    # never fatal: a module that builds its settings at import cannot be imported without its deployment's
    # environment, and failing the build on that would not be a packaging check.
    for line in executed:
        print(f"import gate: NOT a packaging problem, module executed and raised — {line}")

    attempted = len(modules) + len(failures) + len(executed)
    if failures:
        print(f"import gate FAILED — {len(failures)}/{attempted} modules could not be IMPORTED:")
        for line in failures:
            print(f"  {line}")
        return 1

    print(f"import gate: {len(modules)} modules OK ({', '.join(packages)})")
    return 0


def _is_packaging_failure(exc: BaseException) -> bool:
    """True when the failure is a missing/unloadable module — the thing this gate exists to catch.

    Walks `__cause__`/`__context__` because the interesting case is almost always WRAPPED: a module
    that does `from x import y` inside a try, or a library that re-raises an ImportError as its own
    error type, would otherwise be classified as "executed" and let a genuinely missing dependency
    through. The chain is walked with a seen-set because `__context__` can cycle.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImportError):  # ModuleNotFoundError is a subclass
            return True
        current = current.__cause__ or current.__context__
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
