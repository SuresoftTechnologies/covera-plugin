"""Process-owned project lock shared by new and resumed workflow invocations."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path

from cover_build.core.errors import ExecutorError


class ProjectBusy(ExecutorError):
    pass


@contextmanager
def acquire(project_root: str | Path, *, agent_config: bool = False):
    root = Path(project_root).expanduser().resolve(strict=True)
    directory = root / ".cover-build"
    directory.mkdir(exist_ok=True)
    path = directory / "workflow.lock"
    # Gradle can hash every project file. Lock beyond the empty file's EOF
    # so a reader can read it while competing workflows still exclude each other.
    offset = 0 if agent_config else 1 << 20
    # Never unlink this file: waiters must always lock the same filesystem object.
    # The OS releases the lock when the owning process closes the handle/exits.
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(offset)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise ProjectBusy(
                "PROJECT_BUILD_ALREADY_RUNNING",
                "Another COVER workflow is running for this project. Wait for it to finish before resuming; do not start another build or delete the lock file.",
                [str(root), str(path)],
            ) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(offset)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
