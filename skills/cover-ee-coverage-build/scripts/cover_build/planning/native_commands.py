"""Derive coverage commands from the validated native build, without rebuilding argv."""

from __future__ import annotations

import copy
import os
import re
import shutil
from pathlib import Path

from cover_build.common import fileio, pathutil
from cover_build.core.errors import WorkflowWaiting


def _command(source, generated, build_tool):
    # A shell/script is not the native tool: appending its options would silently
    # change the customer's script interface. Ask for the actual invocation.
    if not pathutil.same_path(source["executable"], build_tool):
        raise WorkflowWaiting(
            "NATIVE_WRAPPER_INJECTION_UNRESOLVED",
            "Preserve the customer script's setup and expose its native build invocation before injecting wrapper options",
            {"originalBuildPlan": []},
            decision_owner="MODEL", purpose="ORIGINAL_BUILD_COMMAND",
            evidence=[source["executable"]],
        )
    result = copy.deepcopy(source)
    delta = dict(source.get("environmentDelta", {}))
    for name, value in generated.get("environmentDelta", {}).items():
        if name == "LD_LIBRARY_PATH":
            original_libraries = delta.get(name, source.get("environment", {}).get(name))
            if original_libraries is not None:
                # Add the Agent library directory without substituting the
                # runner's inherited search path for the customer's path.
                agent_library = value.split(os.pathsep, 1)[0]
                value = os.pathsep.join(part for part in (agent_library, original_libraries) if part)
        if os.name == "nt":
            delta = {key: item for key, item in delta.items() if key.casefold() != name.casefold()}
        delta[name] = value
    result["environmentDelta"] = delta
    return result


def _make_injection_values(command, plan, original, injected):
    """Preserve literal tool arguments; do not flatten indirect or scoped assignments."""
    entry = original.get("adapterEntry")
    if not entry or not plan.get("realTools"):
        return injected
    lines = Path(entry).read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    env = {**(command.get("environment") if command.get("environmentMode") == "replace" else os.environ),
           **command.get("environmentDelta", {})}
    roles = {name: role for role, name in plan["toolVariables"].items()}
    def unresolved(name):
        raise WorkflowWaiting("NATIVE_WRAPPER_INJECTION_UNRESOLVED",
            f"Cannot safely replace {name}; review its effective Make definition or select a dedicated tool variable",
            {"originalBuildPlan": [], "originalBuildCommandSource": ["MODEL_INFERRED"]},
            decision_owner="MODEL", purpose="ORIGINAL_BUILD_COMMAND", evidence=[entry])
    result = []
    for arg in injected:
        name, replacement = arg.split("=", 1)
        key = re.escape(name)
        literal = re.compile(rf"^\s*{key}\s*(?::=|=)\s*(.*)$")
        values = []
        uncertain = False
        depth = 0
        for line in lines:
            if line.startswith("\t") or line.lstrip().startswith("#"):
                continue
            if re.match(r"^\s*(?:ifeq|ifneq|ifdef|ifndef)\b", line):
                depth += 1
            elif re.match(r"^\s*endif\b", line):
                depth = max(0, depth - 1)
            match = literal.match(line)
            if match:
                values.append(match[1].partition("#")[0].rstrip())
                uncertain |= bool(depth)
            elif re.search(rf"(?:^|[:\s]){key}\s*[+:?!]*=", line):
                uncertain = True
            elif re.match(r"^\s*(?:-?include|sinclude)\s", line):
                uncertain = True
        explicit = [a for a in command["arguments"] if re.match(rf"^{key}[:+?!]*=", a)]
        if uncertain or len(set(values)) > 1:
            unresolved(name)
        if explicit and not re.match(rf"^{key}(?::=|=)", explicit[-1]):
            unresolved(name)
        value = (explicit[-1].split("=", 1)[1] if explicit else
                 values[-1] if values else env.get(name))
        if value is not None:
            match = re.fullmatch(r"""(?:"([^"]+)"|'([^']+)'|([^\s'"]+))([ \t]+.*)?""", value.strip())
            if not match or re.search(r"[&|;<>`$\r\n]", value) or value.endswith("\\"):
                unresolved(name)
            tool = next(v for v in match.groups()[:3] if v is not None)
            resolved = (str(Path(command["workingDirectory"]) / tool) if "/" in tool or "\\" in tool
                        else shutil.which(tool, path=env.get("PATH")))
            if not resolved or not pathutil.same_path(resolved, plan["realTools"][roles[name]]):
                unresolved(name)
            replacement += match[4] or ""
        result.append(f"{name}={replacement}")
    return result


def _before_separator(arguments, additions):
    result = list(arguments)
    index = result.index("--") if "--" in result else len(result)
    result[index:index] = additions
    return result


def _cmake_definitions(arguments, values):
    """Replace only selected -D definitions, including split and typed forms."""
    result = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        split = token == "-D" and index + 1 < len(arguments)
        definition = arguments[index + 1] if split else token[2:] if token.startswith("-D") else ""
        key = definition.split("=", 1)[0].split(":", 1)[0]
        if "=" in definition and key in values:
            index += 2 if split else 1
            continue
        result.append(token)
        index += 1
    return _before_separator(result, [f"-D{key}={value}" for key, value in values.items()])


def _msbuild_properties(arguments, values):
    """Keep unrelated properties even when several share one /p: argument."""
    result = []
    folded = {key.casefold() for key in values}
    for token in arguments:
        match = re.match(r"^([/-](?:p|property):)(.*)$", token, re.I)
        if match:
            parts = match[2].split(";")
            kept = [part for part in parts if part.split("=", 1)[0].casefold() not in folded]
            if len(kept) == len(parts):
                result.append(token)
            elif kept:
                result.append(match[1] + ";".join(kept))
        else:
            result.append(token)
    return result + [f"/p:{key}={value}" for key, value in values.items()]


def _msbuild_rebuild(arguments, *, separate_clean=False):
    if separate_clean:
        return list(arguments)
    result = []
    target_options = [re.match(r"^([/-](?:t|target):)(.*)$", token, re.I) for token in arguments]
    targets = [target for match in target_options if match for target in re.split(r"[;,]", match[2])]
    def scope(target):
        return target.rsplit(":", 1)[0] + ":" if ":" in target else ""

    build_scopes = {scope(target).casefold() for target in targets
                    if target.rsplit(":", 1)[-1].casefold() in {"build", "rebuild"}}
    cleaned = set()
    rebuilt = set()
    for token in arguments:
        match = re.match(r"^([/-](?:t|target):)(.*)$", token, re.I)
        if match:
            pieces = re.split(r"([;,])", match[2])
            for index in range(0, len(pieces), 2):
                target = pieces[index]
                prefix = scope(target)
                key = prefix.casefold()
                action = target.rsplit(":", 1)[-1].casefold()
                if action == "clean":
                    cleaned.add(key)
                elif action == "build":
                    if key not in cleaned and "" not in cleaned:
                        pieces[index] = prefix + "Rebuild"
                elif action == "rebuild":
                    rebuilt.add(key)
                elif target and not build_scopes and key not in rebuilt:
                    # A custom target (e.g. Package) is not evidence of a clean
                    # build. Keep it, but rebuild its project first.
                    pieces[index] = prefix + "Rebuild;" + target
                    rebuilt.add(key)
            token = match[1] + "".join(pieces)
        result.append(token)
    if not any(target_options):
        result.append("/t:Rebuild")
    return result


def _msbuild_coverage_properties(arguments, plan):
    """Retain selected coverage configuration and the existing toolset defaults."""
    existing = {}
    for token in arguments:
        match = re.match(r"^[/-](?:p|property):(.*)$", token, re.I)
        if match:
            for part in match[1].split(";"):
                key, separator, value = part.partition("=")
                if separator:
                    existing[key.casefold()] = value
    values = {}
    for name, field in (("Configuration", "configuration"), ("Platform", "platform")):
        selected = plan.get(field)
        if selected is not None and existing.get(name.casefold()) != selected:
            values[name] = selected
    # These were already supplied by plan_vs before command preservation.
    # Explicit customer properties take precedence over captured defaults.
    defaults = list(plan.get("windowsSdkProperties", []))
    if plan.get("vcTargetsPath"):
        defaults.append(f"/p:VCTargetsPath={plan['vcTargetsPath']}{os.sep}")
    for token in defaults:
        match = re.match(r"^[/-](?:p|property):([^=]+)=(.*)$", token, re.I)
        if match and match[1].casefold() not in existing:
            values[match[1]] = match[2]
    values.update(ForceImportBeforeCppTargets=plan["overlay"],
                  CoverWrapperDirectory=plan["wrapperDirectory"])
    return _msbuild_properties(arguments, values)


def _msbuild_diagnostic_log(arguments, original_plan, plan, working_directory):
    # The original diagnostic log is build evidence, not a reusable output.
    # Redirect only that logger; leave all other customer logging options alone.
    original_log = original_plan.get("diagnosticLog")
    if not original_log:
        return arguments
    coverage_log = Path(plan["projectRoot"]) / ".cover-build" / "coverage-msbuild-diagnostic.log"
    result = []
    for token in arguments:
        match = re.match(r"^([/-](?:flp|fileloggerparameters)\d*:)(.*)$", token, re.I)
        if match:
            parts = match[2].split(";")
            for index, part in enumerate(parts):
                key, separator, value = part.partition("=")
                if separator and key.casefold() == "logfile":
                    log_path = Path(value.strip('"'))
                    if not log_path.is_absolute():
                        log_path = Path(working_directory) / log_path
                    if pathutil.same_path(log_path, original_log):
                        parts[index] = f"{key}={coverage_log}"
            token = match[1] + ";".join(parts)
        result.append(token)
    return result


def preserve_native_commands(plan, original_plan, *, configure_arguments=()):
    """Preserve executable, cwd, options and environment; alter only tool injection/rebuild."""
    result = copy.deepcopy(plan)
    originals = original_plan["commands"]
    generated = plan["commands"]
    driver = original_plan["driver"]
    tool = original_plan["buildTool"]
    commands = {}
    if driver == "CMAKE":
        for phase in ("configure", "rebuild"):
            command = _command(originals[phase], generated[phase], tool)
            arguments = command["arguments"]
            if phase == "configure":
                # Already validated by plan_cmake. Keep the established order:
                # original options, then the selected supplemental options.
                arguments = _before_separator(arguments, list(configure_arguments))
            if phase == "configure" and plan.get("toolInjection") != "MSBUILD":
                names = {"c_compiler": "CMAKE_C_COMPILER", "cxx_compiler": "CMAKE_CXX_COMPILER", "ar": "CMAKE_AR", "linker": "CMAKE_LINKER"}
                arguments = _cmake_definitions(arguments, {names[role]: value for role, value in plan["wrappers"].items()})
            if phase == "rebuild":
                front = arguments[:arguments.index("--")] if "--" in arguments else arguments
                flags = ("--verbose",) if "clean" in originals else ("--clean-first", "--verbose")
                arguments = _before_separator(arguments, [flag for flag in flags if flag not in front])
                if plan.get("msbuildProps"):
                    if "--" not in arguments:
                        arguments.append("--")
                    boundary = arguments.index("--") + 1
                    arguments = arguments[:boundary] + _msbuild_properties(arguments[boundary:], {"ForceImportBeforeCppTargets": plan["msbuildProps"]})
            command["arguments"] = arguments
            commands[phase] = command
        if "clean" in originals:
            commands["clean"] = _command(originals["clean"], generated["rebuild"], tool)
    elif driver == "MAKE":
        phases = ("rebuild",) if "rebuild" in originals else ("clean", "build")
        # Reuse the validated injection values, including custom tool variables
        # and the existing native-ar compatibility fallback.
        variable_names = set(plan["toolVariables"].values())
        sample = generated.get("rebuild", generated.get("build"))
        injected = [arg for arg in sample["arguments"] if arg.split("=", 1)[0] in variable_names and "=" in arg]
        for phase in phases:
            command = _command(originals[phase], generated.get(phase, sample), tool)
            arguments = []
            for token in command["arguments"]:
                assignment = re.match(r"^([^:=+?!]+)(?:::?=|[+?!]?=)", token)
                if assignment and assignment[1] in variable_names and any(arg.startswith(assignment[1] + "=") for arg in injected):
                    continue
                arguments.append(token)
            arguments = _before_separator(arguments, _make_injection_values(command, plan, original_plan, injected))
            if phase == "rebuild" and "-B" not in arguments and "--always-make" not in arguments:
                arguments = _before_separator(arguments, ["-B"])
            command["arguments"] = arguments
            commands[phase] = command
        diagnostic = copy.deepcopy(commands.get("rebuild", commands.get("build")))
        diagnostic["arguments"] = _before_separator(diagnostic["arguments"], ["-n", "-B"])
        commands["preflight"] = diagnostic
        if "configure" in originals:
            # A preparatory configure script is executed verbatim, not given
            # Make variable assignments as if it were make itself.
            commands["configure"] = copy.deepcopy(originals["configure"])
    elif driver == "MSBUILD":
        for phase in ("clean", "build"):
            if phase not in originals:
                continue
            source = originals[phase]
            command = _command(source, {}, tool)
            # Match execute_msbuild: only auto-extracted commands use the captured
            # VS environment. Model/user commands retain their own environment mode.
            if original_plan.get("origin") == "SCRIPT_EXTRACTED":
                command["environmentMode"] = "replace"
                command["environment"] = dict(generated["rebuild"].get("environment", {}))
            command["environmentDelta"]["CODESCROLL_TM_INSTALL_PATH"] = plan["agentRoot"]
            arguments = _msbuild_diagnostic_log(source["arguments"], original_plan, plan, source["workingDirectory"])
            if phase == "build":
                arguments = _msbuild_rebuild(arguments, separate_clean="clean" in originals)
            command["arguments"] = _msbuild_coverage_properties(arguments, plan)
            commands["rebuild" if phase == "build" else phase] = command
    else:
        raise ValueError(f"Unsupported native command driver: {driver}")
    result["commands"] = commands
    return fileio.add_integrity(result)
