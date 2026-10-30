"""Read the build account HardwareID used to identify its Agent."""

from __future__ import annotations

import re
from pathlib import Path

from .common import RecoveryError


def hardware_id_from_file(path: str | Path | None = None) -> str:
    target = Path(path) if path else Path.home() / ".cstm_HardwareAddress"
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise RecoveryError("HardwareIdMissing", "빌드 계정의 HardwareID 파일이 필요합니다.") from exc
    # Newer Agent versions store this file as a Java-serialized object, not
    # plain text. The contract is the trailing six-byte-pair address, so scan
    # bytes and decode only the matched ASCII token.
    match = re.search(rb"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})\s*$", data)
    if not match:
        raise RecoveryError("HardwareIdAmbiguous", "파일 끝의 HardwareID를 확인할 수 없습니다.")
    return match.group(1).decode("ascii")
