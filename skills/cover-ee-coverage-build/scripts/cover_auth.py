from __future__ import annotations

"""Run a COVER workflow under one interactive, memory-only credential session."""


import argparse
import getpass
import os
import sys
from contextlib import contextmanager
from typing import Iterator

import cover_workflow_runner
import cover_build_executor
from cover_build.common import open_api_credentials


SECRET_NAMES = (
    "COVER_ACCESS_KEY",
)
SESSION_MARKER = "COVER_CREDENTIAL_SESSION"


def prompt_credentials() -> dict[str, str]:
    """Prompt once without echoing secret values; reuse the env var or cached file first."""
    existing = open_api_credentials.resolve_access_key()
    if existing:
        return {"COVER_ACCESS_KEY": existing}
    return {"COVER_ACCESS_KEY": getpass.getpass("COVER Access Key: ")}


@contextmanager
def scoped_credentials(values: dict[str, str]) -> Iterator[None]:
    scoped_names = SECRET_NAMES + (SESSION_MARKER,)
    original = {name: os.environ.get(name) for name in scoped_names}
    try:
        for name in scoped_names:
            os.environ.pop(name, None)
        os.environ.update({name: value for name, value in values.items() if value})
        os.environ[SESSION_MARKER] = "1"
        yield
    finally:
        for name in scoped_names:
            os.environ.pop(name, None)
            if original[name] is not None:
                os.environ[name] = original[name]
        values.clear()


def terminal_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", action="store_true", help="Run one atomic cover_build_executor.py command")
    parser.add_argument("--force-login", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("runner_args", nargs=argparse.REMAINDER, help="Arguments for cover_workflow_runner.py, after --")
    parsed = parser.parse_args(argv)
    runner_args = list(parsed.runner_args)
    if runner_args[:1] == ["--"]:
        runner_args = runner_args[1:]
    if not runner_args:
        parser.error("provide cover_workflow_runner.py arguments after --")
    values = prompt_credentials()
    try:
        with scoped_credentials(values):
            if parsed.executor:
                return cover_build_executor.main(runner_args)
            return cover_workflow_runner.main(runner_args)
    finally:
        values.clear()

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["terminal"]:
        return terminal_main(args[1:])
    return terminal_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
