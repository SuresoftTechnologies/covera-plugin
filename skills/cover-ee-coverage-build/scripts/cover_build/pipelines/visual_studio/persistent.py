"""Reusable Visual Studio setup: property-sheet import into .vcxproj and its verification."""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable

from cover_build.common import pathutil
from cover_build.common import project_edit
from cover_build.core import errors


VS_ENVIRONMENT_NAMES = {
    "path",
    "include",
    "lib",
    "libpath",
    "vcinstalldir",
    "vsinstalldir",
    "vctoolsinstalldir",
    "vctoolsversion",
    "visualstudioversion",
    "windowssdkdir",
    "windowssdkversion",
    "ucrtversion",
    "universalcrtsdkdir",
    "frameworkdir",
    "frameworkversion",
    "frameworkversion64",
    "extensionsdkdir",
    "codescroll_tm_install_path",
    "vscmd_ver",
}


def _relative_reference(owner: Path, target: Path) -> str:
    return os.path.relpath(target, owner.parent).replace("\\", "/")


def _normalized_msbuild_condition(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value)).replace('"', "'").casefold()


def _property_sheet_group_matches(attributes_text: str, condition: str) -> bool:
    attributes = {
        item.group("name").casefold(): html.unescape(item.group("value"))
        for item in re.finditer(
            r'\b(?P<name>Label|Condition)\s*=\s*(?P<quote>["\'])'
            r'(?P<value>.*?)(?P=quote)',
            attributes_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    return (
        attributes.get("label", "").casefold() == "propertysheets"
        and _normalized_msbuild_condition(attributes.get("condition", ""))
        == _normalized_msbuild_condition(condition)
    )


def _matching_property_sheet_group(
    text: str,
    condition: str,
) -> tuple[re.Match[str], bool] | None:
    candidates: list[tuple[re.Match[str], bool]] = []
    for candidate in re.finditer(
        r'(?P<indent>^[ \t]*)<ImportGroup\b(?P<attrs>[^>]*)>'
        r'(?P<body>.*?)</ImportGroup\s*>',
        text,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    ):
        if _property_sheet_group_matches(candidate.group("attrs"), condition):
            candidates.append((candidate, False))
    for candidate in re.finditer(
        r'(?P<indent>^[ \t]*)<ImportGroup\b(?P<attrs>[^>]*?)\s*/\s*>',
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        if _property_sheet_group_matches(candidate.group("attrs"), condition):
            candidates.append((candidate, True))
    if candidates:
        return min(candidates, key=lambda item: item[0].start())
    return None


def _apply_msbuild(
    setup_root: Path,
    config_assets: list[dict[str, Any]],
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    mappings = [
        value
        for value in details.get("projectConfigurations") or []
        if isinstance(value, dict)
        and isinstance(value.get("project"), str)
        and isinstance(value.get("configuration"), str)
        and isinstance(value.get("platform"), str)
    ]
    if not mappings:
        mappings = [
            {
                "project": value,
                "configuration": details.get("configuration"),
                "platform": details.get("platform"),
            }
            for value in details.get("projects") or []
        ]
    if not mappings or any(not item.get("configuration") or not item.get("platform") for item in mappings):
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_PROJECTS_MISSING",
            "Visual Studio persistence requires each selected vcxproj and its verified configuration",
        )
    props = next((Path(item["path"]) for item in config_assets if item["path"].lower().endswith((".props", ".targets"))), None)
    if props is None:
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_PROPS_MISSING",
            "Visual Studio persistence requires the verified COVER props file",
        )
    wrapper_directory = str(Path(details["wrapperDirectory"]).resolve(strict=True))
    props_text = props.read_text(encoding="utf-8-sig")
    if "<CoverWrapperDirectory>" not in props_text:
        # The verified command-line build supplies CoverWrapperDirectory as an
        # MSBuild global property. Visual Studio does not, so the persistent
        # copy must define it before the overlay tests or consumes the value.
        project_open = re.search(r"<Project\b[^>]*>", props_text, flags=re.IGNORECASE)
        if project_open is None:
            raise errors.ExecutorError(
                "PERSISTENT_PROJECT_FORMAT_UNSUPPORTED",
                "Cannot find the Project element while applying the persistent COVER setup",
            )
        fragment = (
            "\n  <PropertyGroup>\n"
            f"    <CoverWrapperDirectory>{html.escape(wrapper_directory)}</CoverWrapperDirectory>\n"
            "  </PropertyGroup>"
        )
        props_text = props_text[: project_open.end()] + fragment + props_text[project_open.end() :]
        props.write_text(props_text, encoding="utf-8", newline="\n")
        for item in config_assets:
            if Path(item["path"]) == props:
                item["size"] = props.stat().st_size
                item["sha256"] = pathutil.sha256_file(props)
    managed_import = re.compile(
        r'<Import\b(?=[^>]*\bLabel\s*=\s*["\']COVER EE persistent integration["\'])'
        r'(?:[^>]*/>|[^>]*>\s*</Import>)[ \t]*(?:\r?\n)?',
        flags=re.IGNORECASE | re.MULTILINE,
    )
    mappings_by_project: dict[Path, list[dict[str, Any]]] = {}
    for mapping in mappings:
        project = Path(mapping["project"]).resolve(strict=True)
        selected = mappings_by_project.setdefault(project, [])
        identity = (str(mapping["configuration"]), str(mapping["platform"]))
        if selected and any(
            (str(item["configuration"]), str(item["platform"])) != identity for item in selected
        ):
            raise errors.ExecutorError(
                "PERSISTENT_MSBUILD_CONFIGURATION_AMBIGUOUS",
                "One Visual Studio publication can select only one configuration per vcxproj",
                [str(project)],
            )
        if not selected:
            selected.append(mapping)

    changes: list[dict[str, Any]] = []
    try:
        for project, project_mappings in mappings_by_project.items():
            text, encoding, bom = project_edit._decode_project_file(project)
            clean_text = managed_import.sub("", text)
            reference = html.escape(_relative_reference(project, props), quote=True)
            newline = "\r\n" if "\r\n" in text else "\n"
            fragments: list[str] = []
            for mapping in project_mappings:
                configuration = str(mapping["configuration"])
                platform = str(mapping["platform"])
                condition = (
                    "'$(Configuration)|$(Platform)' == '"
                    + configuration.replace("'", "''")
                    + "|"
                    + platform.replace("'", "''")
                    + "'"
                )
                group_match = _matching_property_sheet_group(clean_text, condition)
                group = group_match[0] if group_match else None
                self_closing_group = group_match[1] if group_match else False
                import_line = (
                    f'{group.group("indent") if group else "  "}  <Import Project="{reference}" '
                    f'Condition="Exists(\'{reference}\')" Label="COVER EE persistent integration" />'
                )
                if group is not None:
                    if self_closing_group:
                        replacement = (
                            f'{group.group("indent")}<ImportGroup{group.group("attrs")}>{newline}'
                            f'{import_line}{newline}'
                            f'{group.group("indent")}</ImportGroup>'
                        )
                        clean_text = clean_text[: group.start()] + replacement + clean_text[group.end() :]
                    else:
                        body = group.group("body")
                        updated_body = (
                            body.rstrip() + newline + import_line + newline
                            if body.strip()
                            else newline + import_line + newline
                        )
                        clean_text = (
                            clean_text[: group.start("body")]
                            + updated_body
                            + clean_text[group.end("body") :]
                        )
                else:
                    fragments.append(
                        f'  <ImportGroup Label="PropertySheets" '
                        f'Condition="{html.escape(condition, quote=False)}">{newline}'
                        f'{import_line}{newline}'
                        f'  </ImportGroup>{newline}'
                    )
            fragment = "".join(fragments)
            cpp_props = re.search(
                r'<Import\b(?=[^>]*\bProject\s*=\s*["\'][^"\']*Microsoft\.Cpp\.props["\'])[^>]*/>',
                clean_text,
                flags=re.IGNORECASE,
            )
            cpp_targets = re.search(
                r'^[ \t]*<Import\b(?=[^>]*\bProject\s*=\s*["\'][^"\']*Microsoft\.Cpp\.targets["\'])[^>]*/>[ \t]*(?:\r?\n)?',
                clean_text,
                flags=re.IGNORECASE | re.MULTILINE,
            )
            if cpp_props is None or cpp_targets is None:
                raise errors.ExecutorError(
                    "PERSISTENT_PROJECT_FORMAT_UNSUPPORTED",
                    "Visual Studio persistence requires Microsoft.Cpp.props and Microsoft.Cpp.targets imports",
                    [str(project)],
                )
            placement = re.search(
                r'^[ \t]*<(?:PropertyGroup\b(?=[^>]*\bLabel\s*=\s*["\']UserMacros["\'])|'
                r'ItemDefinitionGroup\b|ItemGroup\b|Import\b(?=[^>]*\bProject\s*=\s*["\']'
                r'[^"\']*Microsoft\.Cpp\.targets["\']))',
                clean_text[cpp_props.end() :],
                flags=re.IGNORECASE | re.MULTILINE,
            )
            insertion = cpp_props.end() + placement.start() if placement is not None else cpp_targets.start()
            updated = clean_text[:insertion] + fragment + clean_text[insertion:] if fragment else clean_text
            record = project_edit._apply_file_change(
                setup_root,
                project,
                project_edit._encode_project_file(updated, encoding, bom),
                "MSBUILD_IMPORT",
            )
            if record:
                changes.append(record)
    except BaseException:
        project_edit._restore_project_changes(changes)
        raise
    return changes


def _verify_msbuild_integration(
    config_assets: list[dict[str, Any]],
    details: dict[str, Any],
) -> dict[str, Any]:
    props = next(
        (Path(item["path"]) for item in config_assets if item["path"].lower().endswith((".props", ".targets"))),
        None,
    )
    if props is None or not props.is_file():
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_PROPS_MISSING",
            "The persistent COVER property sheet does not exist",
        )
    wrapper_directory = Path(details["wrapperDirectory"]).resolve(strict=True)
    props_root = ET.parse(props).getroot()
    properties = {
        node.tag.rsplit("}", 1)[-1]: (node.text or "").strip()
        for node in props_root.iter()
        if node.tag.rsplit("}", 1)[-1] in {"CoverWrapperDirectory", "ExecutablePath"}
    }
    if not pathutil.same_path(properties.get("CoverWrapperDirectory", ""), wrapper_directory):
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_WRAPPER_PATH_INVALID",
            "The persistent property sheet does not select the verified COVER wrapper directory",
            [str(props)],
        )
    executable_parts = [part.strip() for part in properties.get("ExecutablePath", "").split(";") if part.strip()]
    if not executable_parts or executable_parts[0] != "$(CoverWrapperDirectory)":
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_EXECUTABLE_PATH_INVALID",
            "VC++ Executable Directories must place the COVER wrapper directory first",
            [str(props)],
        )

    mappings = details.get("projectConfigurations") or [
        {
            "project": value,
            "configuration": details.get("configuration"),
            "platform": details.get("platform"),
        }
        for value in details.get("projects") or []
    ]
    verified_projects: list[dict[str, str]] = []
    for mapping in mappings:
        project = Path(mapping["project"]).resolve(strict=True)
        root_element = ET.parse(project).getroot()
        children = list(root_element)
        cpp_props_index = next(
            (
                index
                for index, child in enumerate(children)
                if child.tag.rsplit("}", 1)[-1] == "Import"
                and str(child.attrib.get("Project", "")).replace("/", "\\").casefold().endswith(
                    "microsoft.cpp.props"
                )
            ),
            None,
        )
        cpp_index = next(
            (
                index
                for index, child in enumerate(children)
                if child.tag.rsplit("}", 1)[-1] == "Import"
                and str(child.attrib.get("Project", "")).replace("/", "\\").casefold().endswith(
                    "microsoft.cpp.targets"
                )
            ),
            None,
        )
        managed: list[tuple[int, ET.Element, ET.Element]] = []
        for index, group in enumerate(children):
            if group.tag.rsplit("}", 1)[-1] != "ImportGroup":
                continue
            for imported in list(group):
                if (
                    imported.tag.rsplit("}", 1)[-1] == "Import"
                    and imported.attrib.get("Label") == "COVER EE persistent integration"
                ):
                    managed.append((index, group, imported))
        configuration = str(mapping["configuration"])
        platform = str(mapping["platform"])
        expected_condition = (
            "'$(Configuration)|$(Platform)' == '"
            + configuration.replace("'", "''")
            + "|"
            + platform.replace("'", "''")
            + "'"
        )
        expected_condition_key = _normalized_msbuild_condition(expected_condition)
        matching = [
            item
            for item in managed
            if _normalized_msbuild_condition(item[1].attrib.get("Condition", ""))
            == expected_condition_key
        ]
        if len(managed) != 1 or len(matching) != 1:
            raise errors.ExecutorError(
                "PERSISTENT_MSBUILD_IMPORT_INVALID",
                "Each selected vcxproj must contain exactly one configuration-specific COVER Property Sheet",
                [str(project), expected_condition],
            )
        group_index, group, imported = matching[0]
        if (
            group.attrib.get("Label") != "PropertySheets"
            or cpp_props_index is None
            or cpp_index is None
            or group_index <= cpp_props_index
            or group_index >= cpp_index
        ):
            raise errors.ExecutorError(
                "PERSISTENT_MSBUILD_IMPORT_ORDER_INVALID",
                "The COVER Property Sheet must be between Microsoft.Cpp.props and Microsoft.Cpp.targets",
                [str(project)],
            )
        imported_path = (project.parent / imported.attrib.get("Project", "")).resolve()
        if not pathutil.same_path(imported_path, props) or not imported_path.is_file():
            raise errors.ExecutorError(
                "PERSISTENT_MSBUILD_IMPORT_TARGET_INVALID",
                "The vcxproj COVER import does not resolve to the current persistent Property Sheet",
                [str(project), str(imported_path)],
            )
        verified_projects.append(
            {"project": str(project), "configuration": configuration, "platform": platform}
        )
    return {
        "verified": True,
        "propertySheet": str(props),
        "wrapperDirectory": str(wrapper_directory),
        "executableDirectoriesFirst": "$(CoverWrapperDirectory)",
        "projects": verified_projects,
    }
