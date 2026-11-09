"""Collect original-build evidence and validate model-owned toolchain decisions.

No executable detection, role inference, or candidate ranking is performed here.
A cited verbose command is documentary evidence, not a process-execution trace.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from cover_build.common import fileio, pathutil, processes
from cover_build.services import archive_policy
from cover_build.core import errors
from cover_build.core.errors import WorkflowWaiting
from cover_build.pipelines.gcc import original

ROLES = {"c_compiler", "cxx_compiler", "linker", "ar"}


def collect(job: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    data = native["data"]
    plan = data["originalBuildPlan"]
    phase = "rebuild" if "rebuild" in data["commands"] else "build"
    result = data["commands"][phase]
    log = Path(data.get("diagnosticLog") or result["log"])
    if result.get("exitCode") != 0 or not log.is_file():
        raise errors.ExecutorError("ORIGINAL_BUILD_EVIDENCE_INVALID", "A successful original build and its full log are required")
    env = original._command_environment(plan["commands"][phase])
    evidence = {
        "schema": "cover-original-toolchain-evidence/v1",
        "jobId": job["jobId"],
        "buildLog": {"path": str(log), "sha256": pathutil.sha256_file(log),
                     "lineCount": sum(1 for _ in processes.iter_output_lines(log))},
        "buildDirectory": data["buildDirectory"],
        "entry": data["entry"],
        "entrySha256": pathutil.sha256_file(Path(data["entry"])),
        "buildPhase": phase,
        "planIntegrity": plan.get("integrity"),
        "requiredRoles": data.get("reviewRequiredRoles", plan["requiredRoles"]),
        "driver": plan["driver"],
        "pathEnvironment": data.get("toolchainPathEnvironment", next((v for k, v in env.items() if k.casefold() == "path"), "")),
        "artifacts": data.get("artifacts", []),
        "selectedReferences": str(Path(job["directories"]["temporary"]) / "selected-build-references.json"),
        "instructions": [
            "The MODEL chooses archiveReview.requiredMode (tu/module); scripts must not infer it from archiveRole or inputKind. Record mode, reason, moduleIntent and evidence for all final links, RSP contents and archive creation commands.",
            "Trace target-source compilations into direct objects or archives consumed by each final EXE/DLL. Startup objects and external uninstrumented objects do not establish direct coverage object input. Original-build evidence describes expected coverage structure, not confirmed server registration.",
            "If target objects arrive only through archives and the final EXE/DLL must be a COVER module, choose tu. With direct target objects and independently managed archive modules, module may be appropriate. Mixed inputs can still need tu when archive TUs belong in the final module. Archive-only input does not force tu if only independent library modules are intended.",
            "COVER creates a final-output module queue only with registered OBJECT inputs; module-mode archive inputs are separate modules and do not themselves satisfy that condition. Neither filenames nor --whole-archive alone decide the mode. Do not assume tu selects only members retained by the linker.",
            "Explain direct object provenance, archive producer/consumer relationships, expected final module composition and requiredMode in reason/moduleIntent. Investigate uncertainty and conflicting target requirements under this Agent-wide option; ask the user only for genuinely missing intent or information. Do not add post-build symbol checks or server Module queries.",
            "KEEP is valid only when both configured and runtime modes satisfy the model-selected requiredMode. Correct conflicting decisions and resume the same job; authorized changes use the existing backup/restart/restore flow.",
            "Read the full build log and relevant recipes; status messages are not commands.",
            "Select every target tool role with an exact command substring and 1-based log line.",
            "Record each command separately with its working directory; do not merge shell segments.",
            "Use the invoked compiler driver for linking when the build invokes it for the final link.",
            "Do not add unobserved C++/ld tools, configure probes, host tools or dry-run-only commands.",
            "For response-file commands, supply responseFiles path/sha256 and explain their contents.",
            "If evidence is insufficient, collect additional evidence; never guess or request user approval merely for this review."
        ],
    }
    evidence = fileio.add_integrity(evidence)
    fileio.atomic_write_json(Path(job["directories"]["temporary"]) / "original-toolchain-evidence.json", evidence)
    return evidence


def _invalid(message: str) -> None:
    raise errors.ExecutorError("INVALID_ORIGINAL_TOOLCHAIN_DECISION", message)


def resolve(job: dict[str, Any], native: dict[str, Any], decision: Any) -> dict[str, Any]:
    evidence = collect(job, native)
    evidence_path = Path(job["directories"]["temporary"]) / "original-toolchain-evidence.json"
    if not isinstance(decision, dict) or decision.get("evidenceDigest") != evidence["integrity"]:
        raise WorkflowWaiting(
            "ORIGINAL_TOOLCHAIN_REVIEW_REQUIRED",
            "The model must interpret original-build evidence and submit originalToolchainDecision before Agent changes",
            {"originalToolchainDecision": {}}, decision_owner="MODEL", purpose="BUILD_ENVIRONMENT",
            evidence=[str(evidence_path)])
    if decision.get("jobId") != job["jobId"]:
        _invalid("Decision belongs to a different job")
    if decision.get("family") not in {"GCC", "MSVC"}:
        _invalid("The model must specify the toolchain family")
    expected_family = {"MSBUILD": "MSVC", "STM32CUBEIDE": "GCC"}.get(evidence["driver"])
    if expected_family and decision["family"] != expected_family:
        _invalid("Toolchain family conflicts with the selected build adapter")
    entries = decision.get("tools")
    if not isinstance(entries, list) or not entries:
        _invalid("tools must be a nonempty array")
    lines = list(processes.iter_output_lines(Path(evidence["buildLog"]["path"])))
    tools, references, drivers = {}, {}, []
    for item in entries:
        if not isinstance(item, dict):
            _invalid("Each tool requires a structured decision")
        role = item.get("role")
        if role not in ROLES or role in tools:
            _invalid("Roles must be supported and unique; resolve target scope explicitly")
        for key in ("path", "invokedAs", "commandText", "workingDirectory", "reason"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                _invalid(f"{role}: {key} is required")
        kind = item.get("invocationKind")
        allowed = {"c_compiler": {"COMPILER"}, "cxx_compiler": {"COMPILER"},
                   "linker": {"COMPILER_DRIVER", "STANDALONE_LINKER"}, "ar": {"ARCHIVER"}}
        if kind not in allowed[role]:
            _invalid(f"{role}: incompatible invocationKind")
        line_number = item.get("line")
        if type(line_number) is not int or not 1 <= line_number <= len(lines):
            _invalid(f"{role}: line must reference the current full build log")
        command = item["commandText"].strip()
        if command not in lines[line_number - 1]:
            _invalid(f"{role}: commandText is not present in the cited log line")
        invoked = item["invokedAs"]
        # Validate the model's command boundary, without finding commands or inferring roles.
        spellings = (invoked, chr(34) + invoked + chr(34), chr(39) + invoked + chr(39))
        if not any(command == v or (command.startswith(v) and command[len(v):len(v)+1].isspace()) for v in spellings):
            _invalid(f"{role}: invokedAs must be the leading executable in commandText")
        cwd = Path(item["workingDirectory"])
        if not cwd.is_absolute() or not cwd.is_dir():
            _invalid(f"{role}: workingDirectory must be an existing absolute directory")
        if not pathutil.same_path(cwd, evidence["buildDirectory"]) and not item.get("workingDirectoryReason"):
            _invalid(f"{role}: explain the command's working directory from recipe/log evidence")
        selected = Path(item["path"])
        if not selected.is_absolute() or not selected.is_file():
            _invalid(f"{role}: selected executable must exist at an absolute path")
        resolved = original._resolve_command_tool(invoked, {"PATH": evidence["pathEnvironment"]}, cwd)
        if resolved is None or not pathutil.same_path(selected, resolved):
            _invalid(f"{role}: selected path does not resolve from the cited invocation")
        response_files = item.get("responseFiles", [])
        if not isinstance(response_files, list):
            _invalid(f"{role}: responseFiles must be an array")
        for response in response_files:
            if not isinstance(response, dict) or not isinstance(response.get("path"), str):
                _invalid(f"{role}: malformed response-file evidence")
            rsp = Path(response["path"])
            if not rsp.is_absolute():
                _invalid(f"{role}: response-file path must be absolute: {rsp}")
            if not rsp.is_file():
                _invalid(f"{role}: response file is missing or not a file: {rsp}")
            try:
                matches = pathutil.sha256_matches_file(rsp, response.get("sha256"))
            except ValueError as exc:
                _invalid(f"{role}: invalid response-file SHA-256 for {rsp}: {exc}")
            if not matches:
                _invalid(f"{role}: response-file SHA-256 mismatch: {rsp}")
        tools[role] = str(selected)
        references[role] = {
            "path": str(selected), "provenance": "MODEL_REVIEWED_BUILD_LOG",
            "evidenceKind": "DOCUMENTARY_NOT_PROCESS_TRACE", "log": evidence["buildLog"],
            "lineNumber": line_number, "line": lines[line_number - 1].rstrip(),
            "commandText": command, "workingDirectory": str(cwd), "reason": item["reason"],
            "invocationKind": kind, "responseFiles": response_files,
        }
        if kind == "COMPILER_DRIVER":
            drivers.append(str(selected))
    required = set(evidence["requiredRoles"])
    if (required & {"c_compiler", "cxx_compiler"}) and not (tools.keys() & {"c_compiler", "cxx_compiler"}):
        _invalid("At least one observed compiler role is required")
    if (required - {"c_compiler", "cxx_compiler"}) - tools.keys():
        _invalid("Required non-compiler roles are unresolved")
    original.reject_cover_wrappers({k: Path(v) for k, v in tools.items()})
    archive_review = archive_policy.review(decision.get("archiveReview"), evidence, lines)
    result = {"archiveReview": archive_review, "family": decision["family"], "tools": tools, "evidence": references,
              "linkDrivers": drivers, "unusedRoles": sorted(ROLES - tools.keys()),
              "requestedRolesNotObserved": sorted(required - tools.keys()),
              "review": {"owner": "MODEL", "status": "VALIDATED", "evidenceDigest": evidence["integrity"],
                         "evidenceKind": "DOCUMENTARY_NOT_PROCESS_TRACE"}}
    if decision["family"] == "GCC":
        # Name prefix is injection metadata, not tool selection or role inference.
        prefix = original._gcc_target_prefix({k: Path(v) for k, v in tools.items()})
        if prefix is not None:
            result["gccTargetPrefix"] = prefix
    fileio.atomic_write_json(Path(job["directories"]["temporary"]) / "validated-original-toolchain.json", fileio.add_integrity(result))
    return result
