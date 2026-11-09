"""Apply model-reviewed JACOV edits to the customer's existing build files."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable

from cover_build.common import project_edit
from cover_build.core import errors
from cover_build.services import project_backup
from cover_build.common import fileio
from cover_build.pipelines.java import inplace


def ensure_sources_restored(job: dict[str, Any]) -> None:
    try:
        inplace.assert_restored(job["projectRoot"])
    except inplace.SourceError as exc:
        raise errors.ExecutorError("JAVA_SOURCE_RESTORE_REQUIRED", str(exc)) from exc


def prepare_in_place(job: dict[str, Any], integration: dict[str, Any], tool: Path,
                     java_home: Path, spec: dict[str, Any]) -> tuple[dict, list[str]]:
    """Build a portable command; never embed job paths or configuration secrets."""
    if (integration.get("mode") != "IN_PLACE"
            or not isinstance(integration.get("reason"), str) or not integration["reason"].strip()
            or not isinstance(integration.get("separateOutputUnavailableReason"), str)
            or not integration["separateOutputUnavailableReason"].strip()
            or integration.get("sourcePreparationComplete") is not True or integration.get("files")):
        raise errors.ExecutorError("JAVA_CLI_INTEGRATION_INVALID",
                                   "IN_PLACE requires fixed-source evidence and completed source preparation; build-file edits are not combined")
    try:
        roots = inplace.source_directories(Path(job["projectRoot"]).resolve(strict=True), integration.get("sourceRoots"))
    except (inplace.SourceError, OSError) as exc:
        raise errors.ExecutorError("JAVA_CLI_INTEGRATION_INVALID", str(exc)) from exc
    ensure_sources_restored(job)
    plan = {
        "projectRoot": job["projectRoot"],
        "sourceRoots": [str(p.relative_to(Path(job["projectRoot"]).resolve())) for p in roots],
        "javaExecutable": str(java_home / "bin" / ("java.exe" if os.name == "nt" else "java")),
        "jacovTool": str(tool.resolve()),
        "build": {k: spec[k] for k in ("executable", "arguments", "workingDirectory")},
    }
    plan["build"]["timeoutSeconds"] = int(spec.get("timeoutSeconds", 3600))
    plan_path = Path(job["directories"]["temporary"]) / "java-inplace-plan.json"
    fileio.atomic_write_json(plan_path, plan)
    helper = Path(inplace.__file__).resolve()
    command = {**spec, "executable": sys.executable, "arguments": [str(helper), str(plan_path)],
               "timeoutSeconds": plan["build"]["timeoutSeconds"] * (len(roots) + 1) + 120}
    return command, [str(helper), str(plan_path)]


def prepare(job: dict[str, Any], integration: Any, tool: Path) -> tuple[Any, list[bytes]]:
    """Validate every edit before backing up or changing any customer file.

    The model locates generation, instrumentation and compilation boundaries.
    This function deliberately does not guess shell/Ant semantics using regexes.
    """
    def invalid(message: str) -> None:
        raise errors.ExecutorError("JAVA_CLI_INTEGRATION_INVALID", message)

    if isinstance(integration, dict) and integration.get("mode", "SEPARATE_OUTPUT") != "SEPARATE_OUTPUT":
        invalid("Unknown Java CLI integration mode")
    if not isinstance(integration, dict) or not isinstance(integration.get("files"), list) or not integration["files"]:
        invalid("javaCliIntegration.files must contain reviewed build-file edits")
    if not isinstance(integration.get("reason"), str) or not integration["reason"].strip():
        invalid("Explain the source-generation, JACOV and compilation order")
    root = Path(job["projectRoot"]).resolve(strict=True)
    paths, updated = [], []
    for item in integration["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            invalid("Each edit requires a project-relative build-file path")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            invalid("Build-file paths must stay below projectRoot")
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path in paths:
            invalid("Build-file paths must be unique existing project files")
        if path.suffix.lower() in {".java", ".class", ".jar", ".war"} or any(
            part in {".git", ".cover-build", ".cover-ee"} for part in relative.parts
        ):
            invalid("Only customer build files may be edited, never Java sources or job evidence")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise errors.ExecutorError("PROJECT_FILE_CONCURRENT_CHANGE", "Build file changed after review", [str(path)])
        text, encoding, bom = project_backup._decode(path, raw)
        edits = item.get("replacements")
        if not isinstance(edits, list) or not edits:
            invalid("Each build file requires exact before/after replacements")
        for edit in edits:
            if not isinstance(edit, dict):
                invalid("A replacement must be an object")
            before, after = edit.get("before"), edit.get("after")
            if not isinstance(before, str) or not before or not isinstance(after, str) or before == after:
                invalid("A replacement requires different nonempty before and string after values")
            if text.count(before) != 1:
                invalid("Each before fragment must match exactly once")
            text = text.replace(before, after, 1)
        if project_edit._contains_path_reference(text, job["jobDirectory"]):
            invalid("Retained build files may not reference a disposable job directory")
        paths.append(path)
        updated.append(project_backup._encode(text, encoding, bom))
    # The edited build must reference the selected tool, not an unrelated CLI.
    tool_name = str(tool.resolve()).replace("\\", "/")
    if not any(tool_name in project_backup._decode(path, data)[0].replace("\\", "/")
               for path, data in zip(paths, updated)):
        invalid("The edited build must reference the selected JACOV CLI absolute path")
    patch = project_backup.ProjectFilePatch.create(job, "JAVA_CLI", paths)
    by_path = {str(path): data for path, data in zip(paths, updated)}
    return patch, [by_path[record["path"]] for record in patch.records]


def execute(patch: Any, updated: list[bytes], build: Callable[[Callable], dict]) -> tuple[dict, list[dict]]:
    """Back up, apply, verify, and retain; restore before original-build recovery."""
    patch.backup()
    # Check all side-by-side backup conflicts before changing any build file.
    for record in patch.records:
        target = Path(record["path"])
        backup = target.with_name(target.name + ".cover-ee-backup")
        if backup.exists() and (not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != record["sha256"]):
            raise errors.ExecutorError("PROJECT_FILE_BACKUP_CONFLICT", "Existing backup differs from the reviewed original", [str(backup)])
    for record in patch.records:
        target = Path(record["path"])
        backup = target.with_name(target.name + ".cover-ee-backup")
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(Path(record["backupPath"]).read_bytes())
    restored = False
    modes = {record["path"]: stat.S_IMODE(Path(record["path"]).stat().st_mode) for record in patch.records}

    def restore() -> None:
        nonlocal restored
        if not restored:
            patch.restore()
            for path, mode in modes.items():
                os.chmod(path, mode)
            restored = True

    try:
        for record, data in zip(patch.records, updated):
            patch._write_patched(record, data)
            os.chmod(record["path"], modes[record["path"]])
        patch._write_manifest("PATCHED")
        result = build(restore)
        records = patch.promote_side_by_side()
        return result, records
    except BaseException:
        restore()
        raise
