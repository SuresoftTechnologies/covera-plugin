"""STM32CubeIDE managed-build discovery and headless command construction."""

from __future__ import annotations

import os
import shutil
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any


from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.pipelines.gcc import original as gcc_original
from cover_build.common import processes as native_common
from cover_build.common import envelopes
from cover_build.common import native_logs
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors


HEADLESS_APPLICATION = "org.eclipse.cdt.managedbuilder.core.headlessbuild"


def select_project(discovery: dict[str, Any], choices: dict[str, Any]) -> dict[str, Any]:
    projects = list(discovery.get("projects") or [])
    if not projects:
        raise errors.ExecutorError(
            "STM32CUBEIDE_PROJECT_NOT_FOUND",
            "No confirmed STM32CubeIDE .project/.cproject pair was found",
            list(discovery.get("candidateEvidence") or []),
        )
    requested_project = choices.get("stm32CubeIDEProject")
    if requested_project:
        matching = [
            item
            for item in projects
            if str(item.get("projectName")).casefold() == str(requested_project).casefold()
            or pathutil.same_path(str(item.get("projectDirectory")), str(requested_project))
        ]
        if len(matching) != 1:
            raise errors.ExecutorError(
                "STM32CUBEIDE_PROJECT_SELECTION_INVALID",
                "The selected STM32CubeIDE project does not uniquely match discovery evidence",
                [requested_project, *[item.get("projectName") for item in projects]],
            )
        project = matching[0]
    elif len(projects) == 1:
        project = projects[0]
    else:
        raise WorkflowWaiting(
            "STM32CUBEIDE_PROJECT_SELECTION_REQUIRED",
            "More than one STM32CubeIDE project is available",
            {"stm32CubeIDEProject": [item["projectName"] for item in projects]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[item["projectFile"] for item in projects],
        )

    configurations = list(project.get("configurations") or [])
    requested_configuration = choices.get("stm32CubeIDEConfiguration")
    if requested_configuration:
        matches = [value for value in configurations if value.casefold() == str(requested_configuration).casefold()]
        if len(matches) != 1:
            raise errors.ExecutorError(
                "STM32CUBEIDE_CONFIGURATION_INVALID",
                "The selected STM32CubeIDE configuration was not found in .cproject",
                [requested_configuration, *configurations],
            )
        configuration = matches[0]
    elif len(configurations) == 1:
        configuration = configurations[0]
    else:
        raise WorkflowWaiting(
            "STM32CUBEIDE_CONFIGURATION_REQUIRED",
            "The STM32CubeIDE project has multiple build configurations and no customer selection evidence",
            {"stm32CubeIDEConfiguration": configurations},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[project["cprojectFile"]],
        )
    return {**project, "selectedConfiguration": configuration}


def find_headless_executable(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        try:
            selected = Path(explicit).expanduser().resolve(strict=True)
        except OSError as exc:
            raise errors.ExecutorError(
                "STM32CUBEIDE_EXECUTABLE_INVALID",
                "The explicitly selected STM32CubeIDE headless executable is not a file",
                [explicit],
            ) from exc
        if not selected.is_file():
            raise errors.ExecutorError(
                "STM32CUBEIDE_EXECUTABLE_INVALID",
                "The explicitly selected STM32CubeIDE headless executable is not a file",
                [explicit],
            )
        return selected
    for name in ("stm32cubeidec.exe", "stm32cubeide.exe", "stm32cubeide"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    if os.name == "nt":
        for base in (Path("C:/ST"), Path("C:/Program Files/STMicroelectronics")):
            if not base.is_dir():
                continue
            candidates.extend(base.glob("STM32CubeIDE*/STM32CubeIDE/stm32cubeidec.exe"))
            candidates.extend(base.glob("STM32CubeIDE*/stm32cubeidec.exe"))
    else:
        for base in (Path("/opt/st"), Path("/usr/local/st")):
            if base.is_dir():
                candidates.extend(base.glob("stm32cubeide*/stm32cubeide"))
    unique: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and not any(pathutil.same_path(resolved, value) for value in unique):
            unique.append(resolved)
    if not unique:
        raise WorkflowWaiting(
            "STM32CUBEIDE_EXECUTABLE_REQUIRED",
            "STM32CubeIDE headless executable was not found in PATH or standard installation roots",
            {"stm32CubeIDEExecutable": []},
            decision_owner="USER",
            purpose="CUSTOMER_CONFIGURATION",
        )
    if len(unique) > 1:
        raise WorkflowWaiting(
            "STM32CUBEIDE_EXECUTABLE_SELECTION_REQUIRED",
            "More than one STM32CubeIDE installation was found",
            {"stm32CubeIDEExecutable": [str(path) for path in unique]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[str(path) for path in unique],
        )
    return unique[0]


def command_spec(
    executable: Path,
    workspace: Path,
    project: dict[str, Any],
    *,
    import_project: bool,
    wrapper_directory: Path | None = None,
    environment_delta: dict[str, str] | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    arguments = [
        "--launcher.suppressErrors",
        "-nosplash",
        "-application",
        HEADLESS_APPLICATION,
        "-data",
        str(workspace),
    ]
    if import_project:
        arguments.extend(["-import", project["projectDirectory"]])
    arguments.extend(
        [
            "-cleanBuild",
            f"{project['projectName']}/{project['selectedConfiguration']}",
            "-no-indexer",
            "-printErrorMarkers",
            "-verbose",
        ]
    )
    environment: dict[str, str] = dict(environment_delta or {})
    if wrapper_directory is not None:
        arguments.extend(["-Ep", f"PATH={wrapper_directory}"])
        environment["PATH"] = os.pathsep.join([str(wrapper_directory), os.environ.get("PATH", "")])
    spec = processes.command_spec(executable, arguments, Path(project["projectDirectory"]), timeout)
    spec["environmentDelta"] = environment
    return spec


def required_roles(project: dict[str, Any]) -> list[str]:
    commands = " ".join(str(value).casefold() for value in project.get("toolCommands") or [])
    roles = ["c_compiler", "linker"]
    if "g++" in commands:
        roles.append("cxx_compiler")
    return roles


def native_tool_directory(executable: Path) -> Path:
    names = ("arm-none-eabi-gcc.exe", "arm-none-eabi-gcc")
    roots = [executable.parent, executable.parent.parent]
    candidates: list[Path] = []
    for root in roots:
        plugins = root / "plugins"
        if not plugins.is_dir():
            continue
        for name in names:
            for compiler in plugins.glob(f"*gnu-tools-for-stm32*/tools/bin/{name}"):
                resolved = compiler.resolve()
                if resolved.is_file() and not any(pathutil.same_path(resolved, item) for item in candidates):
                    candidates.append(resolved)
    if len(candidates) != 1:
        raise WorkflowWaiting(
            "STM32CUBEIDE_NATIVE_TOOLCHAIN_REQUIRED",
            "The STM32CubeIDE installation does not resolve to one bundled ARM GCC tool directory",
            {"stm32CubeIDEToolDirectory": [str(path.parent) for path in candidates]},
            decision_owner="MODEL" if candidates else "USER",
            purpose="BUILD_ENVIRONMENT" if candidates else "CUSTOMER_CONFIGURATION",
            evidence=[str(path) for path in candidates],
        )
    return candidates[0].parent


def make_tool_directory(executable: Path) -> Path:
    names = ("make.exe", "make")
    roots = [executable.parent, executable.parent.parent]
    candidates: list[Path] = []
    for root in roots:
        plugins = root / "plugins"
        if not plugins.is_dir():
            continue
        for name in names:
            for make in plugins.glob(f"*externaltools.make*/tools/bin/{name}"):
                resolved = make.resolve()
                if resolved.is_file() and not any(pathutil.same_path(resolved, item) for item in candidates):
                    candidates.append(resolved)
    if len(candidates) != 1:
        raise WorkflowWaiting(
            "STM32CUBEIDE_MAKE_TOOL_DIRECTORY_REQUIRED",
            "The STM32CubeIDE installation does not resolve to one bundled make tool directory",
            {"stm32CubeIDEMakeToolDirectory": [str(path.parent) for path in candidates]},
            decision_owner="MODEL" if candidates else "USER",
            purpose="BUILD_ENVIRONMENT" if candidates else "CUSTOMER_CONFIGURATION",
            evidence=[str(path) for path in candidates],
        )
    return candidates[0].parent


def _persistent_host_path_entries() -> list[str]:
    if os.name == "nt":
        try:
            import winreg

            values: list[str] = []
            locations = (
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                (winreg.HKEY_CURRENT_USER, r"Environment"),
            )
            for hive, key_name in locations:
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _ = winreg.QueryValueEx(key, "Path")
                except OSError:
                    continue
                values.extend(os.path.expandvars(str(value)).split(os.pathsep))
            if any(values):
                return values
        except (ImportError, OSError):
            pass
    return os.environ.get("PATH", "").split(os.pathsep)


def persistent_ide_base_path(executable: Path) -> str:
    """Recreate the tool paths contributed by STM32CubeIDE before persisting PATH."""
    entries = [
        str(native_tool_directory(executable)),
        str(make_tool_directory(executable)),
        *_persistent_host_path_entries(),
        str(executable.parent),
    ]
    unique: list[str] = []
    normalized: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        key = entry.rstrip("\\/").replace("/", "\\").casefold()
        if key in normalized:
            continue
        normalized.add(key)
        unique.append(entry)
    return os.pathsep.join(unique)


def _nm_for_compiler(compiler: Path) -> Path:
    suffix = compiler.suffix
    stem = compiler.stem
    if not stem.casefold().endswith("gcc"):
        raise errors.ExecutorError(
            "STM32CUBEIDE_NM_TOOL_NOT_RESOLVED",
            "The observed STM32 C compiler name cannot be mapped to the sibling nm tool",
            [str(compiler)],
        )
    nm = compiler.with_name(f"{stem[:-3]}nm{suffix}")
    if not nm.is_file():
        raise errors.ExecutorError(
            "STM32CUBEIDE_NM_TOOL_NOT_FOUND",
            "The sibling STM32 nm tool was not found",
            [str(nm)],
        )
    return nm


def verify_instrumented_elf_symbols(
    job: dict[str, Any],
    ide_executable: Path,
    artifacts: list[str],
    real_tools: dict[str, str],
) -> dict[str, Any]:
    compiler_value = real_tools.get("c_compiler")
    if not compiler_value:
        raise errors.ExecutorError(
            "STM32CUBEIDE_C_COMPILER_NOT_RESOLVED",
            "The STM32 C compiler is required to verify instrumented ELF symbols",
        )
    compiler = Path(compiler_value)
    if not compiler.is_file():
        native_directory = native_tool_directory(ide_executable)
        compiler = native_directory / compiler.name
        if os.name == "nt" and not compiler.suffix:
            compiler = compiler.with_suffix(".exe")
    nm = _nm_for_compiler(compiler)
    verified: list[dict[str, Any]] = []
    for index, artifact_value in enumerate(artifacts, start=1):
        artifact = Path(artifact_value)
        spec = processes.command_spec(nm, [str(artifact)], artifact.parent, 120)
        result = processes.run_command(
            spec,
            Path(job["directories"]["logs"]) / f"coverage-stm32cubeide-ide-reuse-nm-{index:02d}.log",
        )
        if result["exitCode"] != 0:
            raise errors.ExecutorError(
                "STM32CUBEIDE_NM_FAILED",
                "The STM32 nm tool could not inspect the IDE-reuse output",
                [result["log"], str(artifact)],
            )
        symbols = Path(result["log"]).read_text(encoding="utf-8", errors="replace")
        required = {
            "runtimeInitialization": "cstmInitializeTu" in symbols,
            "translationUnitCoverageData": "cstmCoverageData_" in symbols,
        }
        if not all(required.values()):
            raise errors.ExecutorError(
                "STM32CUBEIDE_INSTRUMENTATION_SYMBOLS_MISSING",
                "The IDE-reuse output does not contain required COVER instrumentation symbols",
                [result["log"], str(artifact), required],
            )
        verified.append(
            {
                "path": str(artifact),
                "requiredSymbols": required,
                "evidence": result["log"],
            }
        )
    return {"nm": str(nm), "artifacts": verified}


def create_original_plan(
    job: dict[str, Any],
    executable: Path,
    workspace: Path,
    project: dict[str, Any],
) -> dict[str, Any]:
    command = command_spec(executable, workspace, project, import_project=True)
    return build_plan.validate_original_build_plan(
        job,
        {
            "schema": build_plan.ORIGINAL_BUILD_PLAN_SCHEMA,
            "jobId": job["jobId"],
            "origin": "SCRIPT_EXTRACTED",
            "evidenceSource": "SCRIPT",
            "environmentInitialization": {"method": "INHERITED_PROCESS"},
            "driver": "STM32CUBEIDE",
            "buildTool": str(executable),
            "adapterEntry": project["projectFile"],
            "originalBuildDirectory": project["projectDirectory"],
            "configuration": project["selectedConfiguration"],
            "targets": [f"{project['projectName']}/{project['selectedConfiguration']}"],
            "requiredRoles": required_roles(project),
            "commands": {"build": command},
            "coverageInjection": {"strategy": "STM32CUBEIDE_HEADLESS_PATH_PREPEND"},
            "cleanBuild": True,
            "evidence": [project["projectFile"], project["cprojectFile"]],
        },
        "STM32CUBEIDE",
    )


def execute_original(job: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    validated = build_plan.validate_original_build_plan(job, plan, "STM32CUBEIDE")
    result = processes.run_command(
        validated["commands"]["build"],
        Path(job["directories"]["logs"]) / "original-stm32cubeide-headless.log",
    )
    if result["exitCode"] != 0:
        raise errors.ExecutorError(
            "ORIGINAL_BUILD_FAILED",
            "STM32CubeIDE headless original clean build failed",
            [result["log"]],
        )
    environment = dict(os.environ)
    environment.update(validated["commands"]["build"].get("environmentDelta") or {})
    native_bin = native_tool_directory(Path(validated["buildTool"]))
    environment["PATH"] = os.pathsep.join([str(native_bin), environment.get("PATH", "")])
    build_directory = Path(validated["originalBuildDirectory"]) / str(validated["configuration"])
    toolchain = gcc_original.extract_gcc_toolchain(
        Path(result["log"]),
        None,
        list(validated["requiredRoles"]),
        environment=environment,
        working_directory=build_directory,
    )
    artifacts = gcc_original.extract_gcc_artifacts(Path(result["log"]), build_directory)
    return envelopes.envelope(
        "EXECUTING_ORIGINAL_BUILD",
        "SUCCEEDED",
        {
            "driver": "STM32CUBEIDE",
            "entry": validated["adapterEntry"],
            "buildDirectory": str(build_directory),
            "commands": {"build": native_common._without_output(result)},
            "originalBuildPlan": validated,
            "toolchain": toolchain,
            "artifacts": artifacts,
        },
    )


def execute_coverage(
    job: dict[str, Any],
    executable: Path,
    workspace: Path,
    project: dict[str, Any],
    wrapper_directory: Path,
    wrappers: dict[str, str],
    real_tools: dict[str, str],
    artifacts: list[str],
    agent_root: Path,
    glue_runtime: dict[str, Any],
) -> dict[str, Any]:
    if not artifacts:
        raise errors.ExecutorError(
            "EXPECTED_ARTIFACT_REQUIRED",
            "STM32CubeIDE original build did not resolve an ELF/AXF/OUT artifact",
        )
    workspace.mkdir(parents=True, exist_ok=True)
    before_path = Path(job["directories"]["temporary"]) / "stm32-artifacts-before.json"
    after_path = Path(job["directories"]["temporary"]) / "stm32-artifacts-after.json"
    processes.snapshot_artifacts(SimpleNamespace(artifact=artifacts, output=str(before_path)))
    # The Windows COVER wrapper resolves COVER_BR_CONFIG below the Agent
    # installation's conf directory. Keep the authoritative job-local copy
    # for evidence, but stage a per-job copy there and pass only its basename.
    job_config = Path(glue_runtime["coverConfig"])
    project_key = hashlib.sha256(str(Path(job["projectRoot"]).resolve()).encode("utf-8")).hexdigest()[:12]
    staged_config = agent_root / "conf" / f"cover-glue-compiler-{project_key}.config"
    staged_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(job_config, staged_config)
    spec = command_spec(
        executable,
        workspace,
        project,
        import_project=False,
        wrapper_directory=wrapper_directory,
        environment_delta={
            "CODESCROLL_TM_INSTALL_PATH": str(agent_root),
            "COVER_BR_CONFIG": staged_config.name,
            **dict(glue_runtime.get("environment") or {}),
        },
    )
    spec["coverToolEnvironment"] = True
    try:
        result = processes.run_command(
            spec,
            Path(job["directories"]["logs"]) / "coverage-stm32cubeide-headless.log",
        )
    except BaseException:
        staged_config.unlink(missing_ok=True)
        raise
    if result["exitCode"] != 0:
        # run_command returns normally for a non-zero build exit code.  Do not
        # leave the per-job basename config in the Agent installation in that
        # path; it is only valid for a verified successful coverage build.
        staged_config.unlink(missing_ok=True)
        raise errors.ExecutorError(
            "COVERAGE_BUILD_FAILED",
            "STM32CubeIDE headless coverage clean build failed",
            [result["log"]],
        )
    verification_plan = {
        "wrappers": wrappers,
        "wrapperVerificationRoles": sorted(wrappers),
        "realTools": real_tools,
        "nativeToolBypassRoles": [],
        "commands": {"preflight": spec},
    }
    try:
        wrapper_verification = native_logs.verify_gcc_preflight_text(
            verification_plan,
            Path(result["log"]),
            conservative_native_tokens=False,
        )
        processes.snapshot_artifacts(SimpleNamespace(artifact=artifacts, output=str(after_path)))
        artifact_verification = processes.verify_artifacts(
            SimpleNamespace(before=str(before_path), after=str(after_path), output=None)
        )
    except BaseException:
        staged_config.unlink(missing_ok=True)
        raise
    build_directory = Path(project["projectDirectory"]) / str(project["selectedConfiguration"])
    observed_artifacts = gcc_original.extract_gcc_artifacts(Path(result["log"]), build_directory)
    observed_paths = [item["path"] for item in observed_artifacts]
    if not observed_paths:
        staged_config.unlink(missing_ok=True)
        raise errors.ExecutorError(
            "COVERAGE_OUTPUT_NOT_OBSERVED",
            "STM32CubeIDE coverage build log did not expose a final ELF/AXF/OUT artifact",
            [result["log"]],
        )
    try:
        comparison = pathutil.require_same_output_paths(artifacts, observed_paths)
    except BaseException:
        staged_config.unlink(missing_ok=True)
        raise
    artifact_verification.setdefault("data", {})["outputPathComparison"] = comparison
    return {
        "command": native_common._without_output(result),
        "commandSpec": spec,
        "persistentAgentConfig": str(staged_config),
        "wrapperVerification": wrapper_verification,
        "artifactVerification": artifact_verification,
        "artifacts": observed_artifacts,
        "outputPathComparison": comparison,
        "glueCodeCompiler": {
            key: glue_runtime[key]
            for key in ("path", "provenance", "mappingPolicy", "configurationMethod", "coverConfig")
            if key in glue_runtime
        },
        "buildInterval": {"startedAt": result["startedAt"], "finishedAt": result["finishedAt"]},
    }


def verify_persistent_ide_build(
    job: dict[str, Any],
    executable: Path,
    project: dict[str, Any],
    wrapper_directory: Path,
    wrappers: dict[str, str],
    real_tools: dict[str, str],
    artifacts: list[str],
    agent_root: Path,
) -> dict[str, Any]:
    """Prove that project-persisted CDT environment works without ``-Ep`` injection."""
    workspace = Path(job["directories"]["temporary"]) / "stm32cubeide-ide-reuse-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    inherited_path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry.rstrip("\\/").replace("/", "\\").casefold()
        != str(wrapper_directory).rstrip("\\/").replace("/", "\\").casefold()
    )
    spec = command_spec(
        executable,
        workspace,
        project,
        import_project=True,
        environment_delta={
            "CODESCROLL_TM_INSTALL_PATH": str(agent_root),
            "COVER_BR_CONFIG": "",
            "PATH": inherited_path,
        },
    )
    spec["coverToolEnvironment"] = True
    before_path = Path(job["directories"]["temporary"]) / "stm32-ide-reuse-artifacts-before.json"
    after_path = Path(job["directories"]["temporary"]) / "stm32-ide-reuse-artifacts-after.json"
    processes.snapshot_artifacts(SimpleNamespace(artifact=artifacts, output=str(before_path)))
    result = processes.run_command(
        spec,
        Path(job["directories"]["logs"]) / "coverage-stm32cubeide-ide-reuse.log",
    )
    if result["exitCode"] != 0:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_IDE_BUILD_FAILED",
            "STM32CubeIDE project-persisted coverage clean build failed without command-line PATH injection",
            [result["log"]],
        )
    processes.snapshot_artifacts(SimpleNamespace(artifact=artifacts, output=str(after_path)))
    artifact_verification = processes.verify_artifacts(
        SimpleNamespace(before=str(before_path), after=str(after_path), output=None)
    )
    build_directory = Path(project["projectDirectory"]) / str(project["selectedConfiguration"])
    observed_artifacts = gcc_original.extract_gcc_artifacts(Path(result["log"]), build_directory)
    observed_paths = [item["path"] for item in observed_artifacts]
    if not observed_paths:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_IDE_OUTPUT_NOT_OBSERVED",
            "STM32CubeIDE IDE-reuse verification did not expose a final ELF/AXF/OUT artifact",
            [result["log"]],
        )
    comparison = pathutil.require_same_output_paths(artifacts, observed_paths)
    artifact_verification.setdefault("data", {})["outputPathComparison"] = comparison
    symbol_verification = verify_instrumented_elf_symbols(job, executable, observed_paths, real_tools)
    wrapper_verification = {
        "verificationMode": "PROJECT_ENVIRONMENT_AND_INSTRUMENTED_ELF",
        "verifiedWrappers": wrappers,
        "commandLinePathInjectionAbsent": "-Ep" not in spec["arguments"],
        "processPathStartsWithWrapper": inherited_path.casefold().startswith(str(wrapper_directory).casefold()),
        "instrumentationSymbols": symbol_verification,
    }
    return {
        "command": native_common._without_output(result),
        "wrapperVerification": wrapper_verification,
        "artifactVerification": artifact_verification["data"],
        "outputPathComparison": comparison,
    }
