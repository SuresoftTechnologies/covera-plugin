"""GCC wrapper preparation, CMake/Make coverage plans, and GNU build log verification."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.common import project_files
from cover_build.common import toolchains
from cover_build.core import errors


GCC_ROLE_ENVIRONMENT = {
    "c_compiler": "CC",
    "cxx_compiler": "CXX",
    "ar": "AR",
    "linker": "LD",
}


def prepare_gcc_wrappers(args: argparse.Namespace) -> tuple[dict[str, Path], Path, Path | None, dict[str, str]]:
    wrappers = {
        name: pathutil.canonical(value)
        for name in GCC_ROLE_ENVIRONMENT
        if (value := getattr(args, name, None))
    }
    if not wrappers:
        raise errors.ExecutorError("GCC_WRAPPER_REQUIRED", "At least one observed GCC wrapper is required")
    wrapper_directories = {path.parent for path in wrappers.values()}
    if len(wrapper_directories) != 1:
        raise errors.ExecutorError("MIXED_AGENT_WRAPPERS", "All GCC wrapper executables must come from one Agent wrapper directory")
    wrapper_directory = next(iter(wrapper_directories))
    if wrapper_directory.name.casefold() != "wrapper":
        raise errors.ExecutorError("INVALID_AGENT_WRAPPER_DIRECTORY", "COVER wrappers must be under <agent-root>/wrapper")
    agent_root = wrapper_directory.parent
    cover_config = None
    config_values = {}
    cover_config_environment = None
    if os.name == "nt":
        cover_config, cover_config_environment = resolve_gcc_config(agent_root, args.cover_config)
        config_values = toolchains.parse_cover_wrapper_config(cover_config)
    glue_compiler_value = getattr(args, "glue_code_compiler", None)
    glue_compiler = pathutil.canonical(glue_compiler_value) if glue_compiler_value else None
    if glue_compiler is not None and pathutil.path_within(glue_compiler, agent_root):
        raise errors.ExecutorError(
            "WRAPPER_SELF_REFERENCE",
            "COVER glue-code compiler must be a real target compiler outside the Agent installation",
            [str(glue_compiler)],
        )
    if glue_compiler is not None and os.name == "nt":
        configured = config_values.get("GlueCodeCompiler", "")
        if not configured or not pathutil.same_path(configured, glue_compiler):
            raise errors.ExecutorError(
                "GLUE_COMPILER_CONFIG_MISMATCH",
                "Windows COVER config must point GlueCodeCompiler at the selected real C++ compiler",
                [{"expected": str(glue_compiler), "actual": configured, "config": str(cover_config)}],
            )
    environment = {"CODESCROLL_TM_INSTALL_PATH": str(agent_root)}
    if os.name == "nt":
        environment["COVER_BR_CONFIG"] = cover_config_environment
    else:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            item for item in (str(agent_root / "lib"), os.environ.get("LD_LIBRARY_PATH", "")) if item
        )
        if glue_compiler is not None:
            environment["COVER_CC"] = str(glue_compiler)
    environment.update({GCC_ROLE_ENVIRONMENT[role]: str(wrapper) for role, wrapper in wrappers.items()})
    return wrappers, agent_root, cover_config, environment


def resolve_gcc_config(agent_root: Path, requested: str) -> tuple[Path, str]:
    """Resolve the Windows GNU wrapper config; Unix wrappers use environment variables."""
    requested_cover_config = str(requested)
    requested_path = Path(requested_cover_config).expanduser()
    if requested_path.is_absolute():
        cover_config = pathutil.canonical(requested_path)
        # The Windows wrapper always prefixes Agent/conf, even for absolute input.
        conf_directory = pathutil.canonical(agent_root / "conf")
        if pathutil.same_path(cover_config.parent, conf_directory):
            cover_config_environment = cover_config.name
        else:
            digest = pathutil.sha256_file(cover_config)
            staged = conf_directory / f"cover-{digest.lower()}.config"
            if staged.exists():
                if pathutil.sha256_file(staged) != digest:
                    raise errors.ExecutorError("COVER_CONFIG_CHANGED", "Staged wrapper config content changed", [str(staged)])
            else:
                shutil.copyfile(cover_config, staged)
            cover_config_environment = staged.name
    else:
        if requested_path.name != requested_cover_config or any(
            separator in requested_cover_config for separator in ("/", "\\")
        ):
            raise errors.ExecutorError(
                "INVALID_COVER_CONFIG",
                "--cover-config must be an absolute path or a file name under <agent-root>/conf",
            )
        cover_config = pathutil.canonical(agent_root / "conf" / requested_cover_config)
        cover_config_environment = cover_config.name
    return cover_config, cover_config_environment


def gcc_runtime_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep COVER runtime settings without using CC/CXX/AR/LD as injection fallback."""
    compiler_variables = set(GCC_ROLE_ENVIRONMENT.values())
    return {name: value for name, value in environment.items() if name not in compiler_variables}


def required_gcc_roles(args: argparse.Namespace, wrappers: dict[str, Path]) -> list[str]:
    roles = list(getattr(args, "require_role", []) or [])
    invalid = sorted(set(roles) - set(wrappers))
    if invalid:
        raise errors.ExecutorError("INVALID_GCC_ROLE", "Unknown required GCC wrapper roles", invalid)
    if not roles:
        raise errors.ExecutorError(
            "GCC_PREFLIGHT_ROLE_REQUIRED",
            "At least one --require-role must be derived from the native command listing",
        )
    return sorted(set(roles))


def gcc_real_tools(args: argparse.Namespace) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in getattr(args, "real_tool", []) or []:
        if "=" not in raw:
            raise errors.ExecutorError("INVALID_GCC_REAL_TOOL", "--real-tool must be ROLE=PATH")
        role, value = raw.split("=", 1)
        role = role.strip()
        if role not in GCC_ROLE_ENVIRONMENT or role in result:
            raise errors.ExecutorError("INVALID_GCC_REAL_TOOL", f"Invalid or duplicate GCC real-tool role: {role}")
        result[role] = str(pathutil.canonical(value.strip()))
    return result


def validate_cmake_configure_args(arguments: list[str]) -> None:
    protected_keys = {
        "CMAKE_BUILD_TYPE",
        "CMAKE_C_COMPILER",
        "CMAKE_CXX_COMPILER",
        "CMAKE_AR",
        "CMAKE_LINKER",
    }
    forbidden_options = {"-S", "-B", "-G", "-A", "-T", "-C", "--preset", "--fresh", "--toolchain"}
    for argument in arguments:
        if not isinstance(argument, str) or not argument:
            raise errors.ExecutorError("INVALID_CMAKE_CONFIGURE_ARGUMENT", "Every CMake configure argument must be a non-empty string")
        option = argument.split("=", 1)[0]
        if option in forbidden_options or any(argument.startswith(prefix) for prefix in ("-S", "-B", "-G", "-A", "-T", "-C")):
            raise errors.ExecutorError(
                "CMAKE_CONFIGURE_OVERRIDE_REJECTED",
                f"Configure argument may override the selected source/build/generator: {argument}",
            )
        if argument.startswith("-U"):
            raise errors.ExecutorError("CMAKE_CONFIGURE_OVERRIDE_REJECTED", f"Cache unsetting is not allowed in a build plan: {argument}")
        if argument == "-D":
            raise errors.ExecutorError(
                "CMAKE_CONFIGURE_OVERRIDE_REJECTED",
                "Standalone -D is not allowed because its value cannot be validated independently",
            )
        if argument.startswith("-D") and "=" in argument:
            key = argument[2:].split(":", 1)[0].split("=", 1)[0]
            if key in protected_keys:
                raise errors.ExecutorError(
                    "CMAKE_CONFIGURE_OVERRIDE_REJECTED",
                    f"Configure argument may override an executor-owned value: {key}",
                )


def plan_cmake(args: argparse.Namespace) -> dict[str, Any]:
    cmake = pathutil.canonical(args.cmake)
    source = pathutil.absolute_literal(args.source) if args.preserve_source_spelling else pathutil.canonical(args.source)
    build = pathutil.canonical(args.build, False)
    project_root = pathutil.canonical(args.project_root or source)
    if args.preserve_source_spelling and not pathutil.same_path(source, project_root):
        raise errors.ExecutorError(
            "SOURCE_ALIAS_TARGET_MISMATCH",
            "Preserved CMake source spelling does not resolve to projectRoot",
            [str(source), str(project_root)],
        )
    wrappers, agent_root, cover_config, wrapper_environment = prepare_gcc_wrappers(args)
    required_roles = required_gcc_roles(args, wrappers)
    real_tools = gcc_real_tools(args)
    if set(required_roles) - set(real_tools):
        raise errors.ExecutorError(
            "INVALID_GCC_REAL_TOOL",
            "Every required CMake wrapper role needs its original-build real-tool path",
            sorted(set(required_roles) - set(real_tools)),
        )
    validate_cmake_configure_args(args.configure_arg)
    configure_args = ["-S", str(source), "-B", str(build)]
    if args.generator:
        configure_args.extend(["-G", args.generator])
    if args.configuration:
        configure_args.append(f"-DCMAKE_BUILD_TYPE={args.configuration}")
    cmake_wrapper_variables = {
        "c_compiler": "CMAKE_C_COMPILER",
        "cxx_compiler": "CMAKE_CXX_COMPILER",
        "ar": "CMAKE_AR",
        "linker": "CMAKE_LINKER",
    }
    configure_args.extend(
        f"-D{cmake_wrapper_variables[role]}={wrapper}"
        for role, wrapper in wrappers.items()
    )
    configure_args.extend(args.configure_arg)
    build_args = ["--build", str(build)]
    if args.configuration:
        build_args.extend(["--config", args.configuration])
    targets = list(getattr(args, "target", []) or [])
    build_command_args = build_args + ["--clean-first", "--verbose", "--parallel", str(args.parallel)]
    if targets:
        build_command_args.extend(["--target", *targets])
    plan = fileio.add_integrity(
        {
            "schema": processes.PLAN_SCHEMA,
            "jobId": envelopes.CURRENT_JOB_ID,
            "adapter": "CMAKE",
            "projectRoot": str(project_root),
            "generator": args.generator,
            "configuration": args.configuration,
            "targets": targets,
            "buildDirectory": str(build),
            # Coverage is executed after the native build and intentionally
            # reuses the project's selected build directory.  The executor
            # removes only CMake-generated state before the coverage
            # configure phase so native compiler cache entries cannot leak
            # into the instrumented build.
            "reuseOriginalBuildDirectory": pathutil.path_within(build, project_root) and not pathutil.same_path(build, project_root),
            "agentRoot": str(agent_root),
            "coverConfig": str(cover_config) if cover_config else None,
            "coverConfigSha256": pathutil.sha256_file(cover_config) if cover_config else None,
            "glueCodeCompiler": {
                "path": str(pathutil.canonical(args.glue_code_compiler)),
                "provenance": getattr(args, "glue_compiler_provenance", None),
                "mappingPolicy": "GLUE_ONLY_NOT_AGENT_WRAPPER_MAPPING",
            }
            if getattr(args, "glue_code_compiler", None)
            else None,
            "wrappers": {key: str(value) for key, value in wrappers.items()},
            "wrapperVerificationRoles": required_roles,
            "realTools": real_tools,
            "nativeToolBypassRoles": [],
            "injectionMethod": "COMMAND_LINE",
            "commands": {
                "configure": processes.command_spec(cmake, configure_args, source, args.timeout),
                "rebuild": processes.command_spec(cmake, build_command_args, source, args.timeout),
            },
            "expectedArtifacts": [str(pathutil.canonical(path, False)) for path in args.artifact],
        }
    )
    for phase in ("configure", "rebuild"):
        plan["commands"][phase]["environmentDelta"] = gcc_runtime_environment(wrapper_environment)
    plan = fileio.add_integrity(plan)
    if args.output:
        fileio.atomic_write_json(pathutil.canonical(args.output, False), plan)
    return envelopes.envelope("PLANNING_BUILD", "SUCCEEDED", plan)


def make_tool_variables(values: list[str] | None = None) -> dict[str, str]:
    result = dict(GCC_ROLE_ENVIRONMENT)
    for item in values or []:
        if not isinstance(item, str) or "=" not in item:
            raise errors.ExecutorError("INVALID_MAKE_TOOL_VARIABLE", "Make tool-variable mappings must use ROLE=NAME")
        role, name = item.split("=", 1)
        if role not in GCC_ROLE_ENVIRONMENT or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise errors.ExecutorError("INVALID_MAKE_TOOL_VARIABLE", "Invalid Make tool-variable mapping", [item])
        result[role] = name
    if len(set(result.values())) != len(result):
        raise errors.ExecutorError("INVALID_MAKE_TOOL_VARIABLE", "Each GCC role must use a distinct Make variable", list(result.values()))
    return result


def make_variable_arguments(
    wrappers: dict[str, Path],
    real_tools: dict[str, Path] | None = None,
    tool_variables: dict[str, str] | None = None,
) -> list[str]:
    # A COVER wrapper invokes its mapped real compiler by name.  Putting the
    # wrapper directory first on PATH therefore recurses into the wrapper. Use
    # an explicit wrapper executable in the Make variable and retain the normal
    # PATH so the wrapper resolves the real compiler.
    variables = tool_variables or dict(GCC_ROLE_ENVIRONMENT)
    result: list[str] = []
    for role, wrapper in wrappers.items():
        value = str(wrapper)
        if any(character.isspace() for character in value):
            if os.name != "nt":
                raise errors.ExecutorError("GCC_WRAPPER_PATH_UNSAFE", f"Make cannot safely expand a whitespace wrapper path: {wrapper}")
            buffer = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetShortPathNameW(value, buffer, len(buffer)):
                raise errors.ExecutorError("GCC_WRAPPER_PATH_UNSAFE", f"No whitespace-free Windows path is available for {wrapper}")
            value = buffer.value
            if any(character.isspace() for character in value):
                raise errors.ExecutorError("GCC_WRAPPER_PATH_UNSAFE", f"Make cannot safely expand a whitespace wrapper path: {wrapper}")
        result.append(f"{variables[role]}={value}")
    if real_tools and "ar" in real_tools:
        # SP8 Windows GCC ar.exe can fail its basic pass-through path. Object
        # instrumentation already occurs in the compiler wrapper, so archive
        # that instrumented object with the mapped native ar.
        result.append(f"{variables['ar']}={real_tools['ar']}")
    return result


def plan_make(args: argparse.Namespace) -> dict[str, Any]:
    make = pathutil.canonical(args.make)
    makefile = pathutil.canonical(args.makefile)
    working_directory = pathutil.canonical(args.working_directory or makefile.parent)
    if not pathutil.same_path(makefile.parent, working_directory) and not str(makefile).casefold().startswith(str(working_directory).casefold()):
        raise errors.ExecutorError("MAKEFILE_OUTSIDE_WORKING_DIRECTORY", f"{makefile} is outside {working_directory}")
    wrappers, agent_root, cover_config, environment = prepare_gcc_wrappers(args)
    required_roles = required_gcc_roles(args, wrappers)
    real_tools = gcc_real_tools(args)
    archive_bypass = bool(getattr(args, "allow_native_ar_fallback", False))
    if archive_bypass and "ar" not in real_tools:
        raise errors.ExecutorError("NATIVE_AR_FALLBACK_UNAVAILABLE", "Native AR fallback requires --real-tool ar=PATH")
    make_wrappers = {role: wrapper for role, wrapper in wrappers.items() if role != "ar" or not archive_bypass}
    tool_variables = make_tool_variables(getattr(args, "tool_variable", None))
    variables = make_variable_arguments(make_wrappers, real_tools if archive_bypass else None, tool_variables)
    file_args = ["-f", str(makefile)]
    targets = list(args.target)
    force_rebuild = bool(getattr(args, "force_rebuild", False))
    build_arguments = file_args + [f"-j{args.parallel}", *variables, *targets]
    if force_rebuild:
        build_arguments.insert(2, "-B")
        build_commands = {
            "rebuild": processes.command_spec(make, build_arguments, working_directory, args.timeout),
        }
    else:
        build_commands = {
            "clean": processes.command_spec(make, file_args + [*variables, args.clean_target], working_directory, args.timeout),
            "build": processes.command_spec(make, build_arguments, working_directory, args.timeout),
        }
    plan = fileio.add_integrity(
        {
            "schema": processes.PLAN_SCHEMA,
            "jobId": envelopes.CURRENT_JOB_ID,
            "adapter": "MAKE_GCC",
            "projectRoot": str(pathutil.canonical(args.project_root or working_directory)),
            "buildDirectory": str(working_directory),
            "reuseOriginalBuildDirectory": pathutil.path_within(
                working_directory,
                pathutil.canonical(args.project_root or working_directory),
            ) and not pathutil.same_path(working_directory, pathutil.canonical(args.project_root or working_directory)),
            "agentRoot": str(agent_root),
            "coverConfig": str(cover_config) if cover_config else None,
            "coverConfigSha256": pathutil.sha256_file(cover_config) if cover_config else None,
            "glueCodeCompiler": {
                "path": str(pathutil.canonical(args.glue_code_compiler)),
                "provenance": getattr(args, "glue_compiler_provenance", None),
                "mappingPolicy": "GLUE_ONLY_NOT_AGENT_WRAPPER_MAPPING",
            }
            if getattr(args, "glue_code_compiler", None)
            else None,
            "wrappers": {key: str(value) for key, value in wrappers.items()},
            "realTools": real_tools,
            "preflightRequiredRoles": [role for role in required_roles if role != "ar" or not archive_bypass],
            "nativeToolBypassRoles": ["ar"] if archive_bypass else [],
            "toolVariables": tool_variables,
            "forceRebuild": force_rebuild,
            "injectionMethod": "COMMAND_LINE",
            "preflightPolicy": "ON_WRAPPER_SELECTION_FAILURE",
            "commands": {
                "preflight": processes.command_spec(make, file_args + ["-n", "-B", *variables, *targets], working_directory, args.timeout),
                **build_commands,
            },
            "expectedArtifacts": [str(pathutil.canonical(path, False)) for path in args.artifact],
        }
    )
    for phase in plan["commands"]:
        plan["commands"][phase]["environmentDelta"] = gcc_runtime_environment(environment)
    plan = fileio.add_integrity(plan)
    if args.output:
        fileio.atomic_write_json(pathutil.canonical(args.output, False), plan)
    return envelopes.envelope("PLANNING_BUILD", "SUCCEEDED", plan)


def verify_cmake_cache(args: argparse.Namespace) -> dict[str, Any]:
    plan = fileio.load_json(args.plan)
    if plan.get("schema") != processes.PLAN_SCHEMA or plan.get("adapter") not in {"CMAKE", "CMAKE_GCC"}:
        raise errors.ExecutorError("INVALID_PLAN_SCHEMA", "verify-cmake-cache requires a CMake build plan")
    fileio.verify_integrity(plan)
    cache_path = pathutil.canonical(args.cache)
    values = project_files.parse_cmake_cache(cache_path)
    generator = values.get("CMAKE_GENERATOR")
    planned_generator = plan.get("generator")
    if planned_generator and generator != planned_generator:
        raise errors.ExecutorError(
            "CMAKE_GENERATOR_MISMATCH",
            "The coverage CMake cache does not preserve the selected generator",
            [{"expected": planned_generator, "actual": generator}],
        )
    cache_keys = {
        "c_compiler": "CMAKE_C_COMPILER",
        "cxx_compiler": "CMAKE_CXX_COMPILER",
        "ar": "CMAKE_AR",
        "linker": "CMAKE_LINKER",
    }
    expected = {cache_keys[role]: path for role, path in plan["wrappers"].items()}
    mismatches = [
        {"key": key, "expected": path, "actual": values.get(key)}
        for key, path in expected.items()
        if not values.get(key) or not pathutil.same_path(values[key], path)
    ]
    if mismatches:
        raise errors.ExecutorError("CMAKE_WRAPPER_CACHE_MISMATCH", "CMakeCache.txt does not select the planned COVER wrappers", mismatches)
    return envelopes.envelope(
        "VERIFYING_CMAKE_CACHE",
        "SUCCEEDED",
        {"cache": str(cache_path), "generator": generator, "verifiedCompilers": expected},
    )
