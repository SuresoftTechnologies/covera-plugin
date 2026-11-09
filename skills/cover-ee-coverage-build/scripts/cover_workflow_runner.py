#!/usr/bin/env python3
"""COVER 워크플로의 공개 CLI. 단계 순서는 cover_build.workflow에서 관리한다."""

from __future__ import annotations

import sys

from cover_build import workflow


def main(argv: list[str] | None = None) -> int:
    return workflow.run(argv)


if __name__ == "__main__":
    sys.exit(main())
