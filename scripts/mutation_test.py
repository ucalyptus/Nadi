#!/usr/bin/env python3
"""Small deterministic mutation test gate for Nadi.

This is not a full mutation-testing framework; it is a fast stdlib gate that
applies targeted semantic mutants to critical invariants and requires the test
suite to fail for each one. It catches regressions around broker bypass, JWT
validation, sandbox path isolation, deterministic runtime behavior, and event
ordering.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    name: str
    path: str
    old: str
    new: str


MUTANTS = [
    Mutant("uppercase_tool_lowercases", "nadi/sandboxd.py", 'str(args.get("text", "")).upper()', 'str(args.get("text", "")).lower()'),
    Mutant("sandbox_escape_allows_parent", "nadi/sandboxd.py", 'if self.root not in [target, *target.parents]:', 'if False and self.root not in [target, *target.parents]:'),
    Mutant("jwt_allows_wrong_session", "nadi/security.py", 'if data.get("sid") != session_id:', 'if False and data.get("sid") != session_id:'),
    Mutant("jwt_allows_missing_scope", "nadi/security.py", 'if scope not in scopes and data.get("scope") != "*":', 'if False and scope not in scopes and data.get("scope") != "*":'),
    Mutant("model_prefix_changed", "nadi/runtime.py", 'f"deterministic-model:{prompt}"', 'f"mutated-model:{prompt}"'),
    Mutant("event_sequence_starts_at_zero", "nadi/store.py", 'SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id=?', 'SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id=?'),
    Mutant("broker_counts_tool_path", "nadi/broker.py", 'self.tool_path_calls = 0  # tests assert this stays zero for tool execution', 'self.tool_path_calls = 1  # mutated: pretend broker entered tool path'),
]


def copy_repo(tmp: Path) -> Path:
    dst = tmp / "repo"
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(ROOT, dst, ignore=ignore)
    return dst


def apply_mutant(repo: Path, mutant: Mutant) -> None:
    path = repo / mutant.path
    text = path.read_text()
    if mutant.old not in text:
        raise RuntimeError(f"mutant {mutant.name}: target text not found in {mutant.path}")
    path.write_text(text.replace(mutant.old, mutant.new, 1))


def run_tests(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for m in MUTANTS:
            print(m.name)
        return 0

    killed = 0
    survivors: list[str] = []
    for mutant in MUTANTS:
        with tempfile.TemporaryDirectory(prefix=f"nadi-mutant-{mutant.name}-") as td:
            repo = copy_repo(Path(td))
            apply_mutant(repo, mutant)
            result = run_tests(repo)
            if result.returncode != 0:
                killed += 1
                print(f"KILLED {mutant.name}")
            else:
                survivors.append(mutant.name)
                print(f"SURVIVED {mutant.name}")
    score = killed / len(MUTANTS) * 100.0
    print(f"Mutation score: {killed}/{len(MUTANTS)} killed ({score:.2f}%)")
    if survivors:
        print("Survivors: " + ", ".join(survivors))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
