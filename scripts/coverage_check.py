#!/usr/bin/env python3
"""Stdlib coverage gate using trace.

Runs unittest discovery under trace and fails when nadi/*.py line coverage is
below the requested threshold. This intentionally avoids third-party packages so
fresh clones can verify coverage with only Python.
"""
from __future__ import annotations

import argparse
import runpy
import sys
import trace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "nadi"


def executable_lines(path: Path) -> set[int]:
    lines: set[int] = set()
    in_docstring = False
    quote = ""
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_docstring:
            if quote in stripped:
                in_docstring = False
            continue
        if stripped.startswith(("'''", '"""')):
            quote = stripped[:3]
            if stripped.count(quote) < 2:
                in_docstring = True
            continue
        lines.add(lineno)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=float, default=75.0, help="minimum package line coverage percentage")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    old_argv = sys.argv[:]
    sys.argv = ["unittest", "discover", "-s", str(ROOT / "tests")]
    tracer = trace.Trace(count=True, trace=False, ignoredirs=[sys.base_prefix, sys.prefix])
    try:
        tracer.runfunc(runpy.run_module, "unittest", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        if exc.code not in (0, None):
            return int(exc.code) if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv

    counts = tracer.results().counts
    total = covered = 0
    rows: list[tuple[str, int, int, float]] = []
    for path in sorted(PACKAGE.glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        executable = executable_lines(path)
        hit = {lineno for (filename, lineno), count in counts.items() if Path(filename).resolve() == path.resolve() and count > 0}
        file_total = len(executable)
        file_covered = len(executable & hit)
        total += file_total
        covered += file_covered
        pct = 100.0 if file_total == 0 else file_covered / file_total * 100.0
        rows.append((rel, file_covered, file_total, pct))

    pct = 100.0 if total == 0 else covered / total * 100.0
    print("Coverage by file:")
    for rel, file_covered, file_total, file_pct in rows:
        print(f"  {rel:28} {file_covered:4d}/{file_total:<4d} {file_pct:6.2f}%")
    print(f"TOTAL                         {covered:4d}/{total:<4d} {pct:6.2f}%")
    if pct < args.min:
        print(f"FAIL: coverage {pct:.2f}% < required {args.min:.2f}%")
        return 2
    print(f"PASS: coverage {pct:.2f}% >= required {args.min:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
