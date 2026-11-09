from __future__ import annotations

"""Create one reusable, bounded inventory for all COVER build detectors."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "cover-project-index/v1"
SOURCE_SUFFIXES = {
    ".c": "C",
    ".cc": "CXX",
    ".cp": "CXX",
    ".cpp": "CXX",
    ".cxx": "CXX",
    ".c++": "CXX",
    ".cs": "CSHARP",
    ".java": "JAVA",
}
ARTIFACT_SUFFIXES = {
    ".exe", ".dll", ".lib", ".a", ".so", ".dylib", ".jar", ".war", ".elf", ".axf", ".out"
}
SKIP_DIRS = {
    ".cover-build", ".cover-ee", ".git", ".gradle", ".hg", ".idea", ".svn", ".vs", "node_modules"
}
BUILD_NAMES = {
    "cmakelists.txt", "cmakepresets.json", "cmakeuserpresets.json", "cmakecache.txt",
    "makefile", "gnumakefile", "build.ninja", "compile_commands.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "gradlew", "gradlew.bat", "mvnw", "mvnw.cmd", ".project", ".cproject",
    "azure-pipelines.yml", "azure-pipelines.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml",
    "jenkinsfile", "configure", "bootstrap", "global.json",
}
BUILD_SUFFIXES = {
    ".sln", ".slnx", ".vcxproj", ".vcproj", ".csproj", ".vbproj", ".fsproj",
    ".props", ".targets", ".bat", ".cmd", ".ps1", ".sh", ".ioc", ".cmake",
}
RUNTIME_NAMES = {
    "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "chart.yaml"
}
MAX_SOURCE_PATHS = 200
MAX_ARTIFACT_CANDIDATES = 1000


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_ci_yaml(relative: Path) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    return (
        len(parts) == 3
        and parts[:2] == (".github", "workflows")
        and relative.suffix.casefold() in {".yml", ".yaml"}
    )


def _is_runtime(relative: Path) -> bool:
    name = relative.name.casefold()
    parts = {part.casefold() for part in relative.parts}
    return (
        name.startswith("dockerfile")
        or name in RUNTIME_NAMES
        or bool(parts & {"deploy", "deployment", "helm", "k8s", "kubernetes", "manifests"})
    )


def build_index(project_root: str | Path, output_directories: Iterable[str] = (),
                *, planned_outputs: bool = False, output_files: Iterable[str] = ()) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    outputs = [Path(value).resolve() for value in output_directories]
    generated_files = {Path(value).resolve() for value in output_files}
    entries: list[dict[str, Any]] = []
    source_counts = {name: 0 for name in sorted(set(SOURCE_SUFFIXES.values()))}
    source_paths = {name: [] for name in source_counts}
    artifact_candidates: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_directories: list[str] = []

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        retained: list[str] = []
        for name in sorted(dirnames, key=str.casefold):
            if name.casefold() in SKIP_DIRS or _inside_outputs(Path(directory) / name, outputs):
                skipped_directories.append((Path(directory) / name).relative_to(root).as_posix())
            else:
                retained.append(name)
        dirnames[:] = retained
        current = Path(directory)
        for filename in sorted(filenames, key=str.casefold):
            scanned_files += 1
            path = current / filename
            if path.resolve() in generated_files:
                continue
            relative = path.relative_to(root)
            relative_text = relative.as_posix()
            lowered = filename.casefold()
            suffix = path.suffix.casefold()
            language = SOURCE_SUFFIXES.get(suffix)
            if language:
                source_counts[language] += 1
                if len(source_paths[language]) < MAX_SOURCE_PATHS:
                    source_paths[language].append(relative_text)
            if suffix in ARTIFACT_SUFFIXES:
                try:
                    stat = path.stat()
                except OSError:
                    pass
                else:
                    artifact_candidates.append(
                        {"path": str(path.resolve()), "relativePath": relative_text, "size": stat.st_size, "modifiedNs": stat.st_mtime_ns}
                    )
            relevant = bool(
                language
                or lowered in BUILD_NAMES
                or suffix in BUILD_SUFFIXES
                or _is_ci_yaml(relative)
                or _is_runtime(relative)
            )
            if relevant:
                try:
                    relevant_stat = path.stat()
                except OSError:
                    relevant_stat = None
                content_sha256 = None
                if relevant_stat is not None and language is None:
                    try:
                        content_sha256 = _sha256_file(path)
                    except OSError:
                        pass
                entries.append(
                    {
                        "path": str(path.resolve()),
                        "relativePath": relative_text,
                        "name": filename,
                        "lowerName": lowered,
                        "suffix": suffix,
                        "language": language,
                        "ciYaml": _is_ci_yaml(relative),
                        "runtimeCandidate": _is_runtime(relative),
                        "size": relevant_stat.st_size if relevant_stat is not None else None,
                        "modifiedNs": relevant_stat.st_mtime_ns if relevant_stat is not None else None,
                        "sha256": content_sha256,
                    }
                )

    entries.sort(key=lambda item: item["relativePath"].casefold())
    artifact_candidate_count = len(artifact_candidates)
    artifact_candidates.sort(key=lambda item: (-int(item["modifiedNs"]), item["path"].casefold()))
    artifact_candidates = artifact_candidates[:MAX_ARTIFACT_CANDIDATES]
    return {
        "schema": SCHEMA,
        "projectRoot": str(root),
        "plannedOutputs": planned_outputs,
        "outputDirectories": [str(value) for value in outputs],
        "outputFiles": sorted(str(value) for value in generated_files),
        "scannedFileCount": scanned_files,
        "entries": entries,
        "sourceCounts": source_counts,
        "sourcePaths": source_paths,
        "sourcePathsTruncated": {
            name: source_counts[name] > len(source_paths[name]) for name in source_counts
        },
        "artifactCandidates": artifact_candidates,
        "artifactCandidateCount": artifact_candidate_count,
        "artifactCandidatesTruncated": artifact_candidate_count > len(artifact_candidates),
        "skippedDirectories": skipped_directories,
    }


def entries(index: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if index.get("schema") != SCHEMA or not isinstance(index.get("entries"), list):
        raise ValueError(f"Expected {SCHEMA}")
    return index["entries"]


def _inside_outputs(path: Path, outputs: Iterable[Path]) -> bool:
    if not outputs:
        return False
    resolved = path.resolve()
    return any(resolved == output or output in resolved.parents for output in outputs)


def validate_output_tracking(value: Any, project_root: str | Path) -> dict[str, Any]:
    """Validate explicit, evidence-backed generated paths for any build driver."""
    if not isinstance(value, dict) or not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("outputTracking requires an evidence-based reason")
    root = Path(project_root).resolve()
    result = {"reason": value["reason"]}
    for key in ("directories", "files"):
        paths = value.get(key)
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise ValueError("outputTracking requires directories and files arrays")
        normalized = []
        for item in paths:
            path = Path(item)
            if not path.is_absolute():
                raise ValueError("Generated paths must be absolute")
            path = path.resolve()
            if path == root or path in root.parents:
                raise ValueError("Cannot exclude the project root or its ancestors")
            # External outputs cannot change this project's inventory.
            if root in path.parents:
                normalized.append(str(path))
        result[key] = sorted(set(normalized))
    return result


def bind_build_outputs(project_root: str | Path, index_path: Path, plan: dict[str, Any]) -> None:
    """Apply the common output contract before any original-build checkpoint."""
    if not index_path.is_file():
        return
    root = Path(project_root).resolve()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    tracking = plan.get("outputTracking")
    if tracking is None:
        if plan.get("driver") != "CMAKE":
            raise ValueError("The build plan must declare outputTracking")
        output = Path(plan["originalBuildDirectory"]).resolve()
        source = Path(plan["adapterEntry"]).resolve().parent
        # An in-source CMake build requires explicit generated-file declarations.
        if output == root or output == source or output in source.parents:
            raise ValueError("In-source builds require explicit outputTracking.files")
        tracking = {"directories": [str(output)], "files": [],
                    "reason": "CMake out-of-source binary directory from the selected plan"}
    tracking = validate_output_tracking(tracking, root)
    outputs = [Path(value) for value in tracking["directories"]]
    output_files = {Path(value) for value in tracking["files"]}
    protected = [Path(plan["adapterEntry"]).resolve()]
    protected += [Path(value).resolve() for value in plan.get("evidence", [])
                  if isinstance(value, str) and Path(value).is_absolute()]
    if any(_inside_outputs(value, outputs) or value in output_files for value in protected):
        raise ValueError("Generated paths overlap the build entry or plan evidence")
    if (index.get("plannedOutputs") and index.get("outputDirectories") == tracking["directories"]
            and index.get("outputFiles", []) == tracking["files"]):
        return
    # Existing source inputs cannot be hidden by declaring a mixed directory.
    for item in entries(index):
        path = Path(item["path"]).resolve()
        if not (_inside_outputs(path, outputs) or path in output_files):
            continue
        cmake_generated = (plan.get("driver") == "CMAKE" and
                           "cmakefiles" in {part.casefold() for part in path.parts})
        if not cmake_generated and (item.get("language") or item.get("lowerName") in
                {"cmakelists.txt", "cmakepresets.json", "cmakeuserpresets.json", "pom.xml",
                 "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}):
            raise ValueError("Generated paths overlap an indexed source/build input: " + str(path))
    index = build_index(root, tracking["directories"], planned_outputs=True,
                        output_files=tracking["files"])
    index["outputTracking"] = tracking
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path.with_suffix(".inputs.json").write_text(
        json.dumps(resume_signature(index), ensure_ascii=False), encoding="utf-8")


def resume_signature(index: dict[str, Any]) -> dict[str, Any]:
    """Check owned inputs and directory membership without rerunning detectors.

    Directory membership detects additions/deletions; content hashes detect edits
    even when an editor preserves size and timestamps. Generated outputs do not
    invalidate an original-build checkpoint merely because clean/build ran.
    """
    root = Path(index["projectRoot"])
    ignored = SKIP_DIRS
    generated_files = {Path(value).resolve() for value in index.get("outputFiles", [])}
    outputs = [Path(value).resolve() for value in index.get("outputDirectories", [])]
    directories: dict[str, list[str]] = {}
    files: dict[str, str | None] = {}
    for directory, children, names in os.walk(root, followlinks=False):
        children[:] = sorted(name for name in children if name.casefold() not in ignored
                             and not _inside_outputs(Path(directory) / name, outputs))
        relative = Path(directory).relative_to(root)
        relevant = [name for name in names if (Path(directory) / name).resolve() not in generated_files and (
            Path(name).suffix.casefold() in SOURCE_SUFFIXES
            or Path(name).suffix.casefold() in BUILD_SUFFIXES
            or name.casefold() in BUILD_NAMES
            or _is_ci_yaml(relative / name) or _is_runtime(relative / name)
        )]
        if relevant:
            directories[relative.as_posix()] = sorted(relevant)
        for name in relevant:
            path = Path(directory) / name
            try:
                files[(relative / name).as_posix()] = _sha256_file(path)
            except OSError:
                files[(relative / name).as_posix()] = None
    return {"projectRoot": str(root), "directories": directories, "files": files}


def load_or_build(project_root: str | Path, path: Path) -> tuple[dict[str, Any], bool]:
    signature_path = path.with_suffix(".inputs.json")
    outputs, output_files, planned = [], [], False
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
        previous = json.loads(signature_path.read_text(encoding="utf-8"))
        if index.get("schema") == SCHEMA and Path(index["projectRoot"]) == Path(project_root):
            outputs = index.get("outputDirectories", [])
            output_files = index.get("outputFiles", [])
            planned = index.get("plannedOutputs", False)
            if previous == resume_signature(index):
                return index, True
    except (OSError, ValueError, KeyError):
        pass
    index = build_index(project_root, outputs, planned_outputs=planned, output_files=output_files)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    signature_path.write_text(json.dumps(resume_signature(index), ensure_ascii=False), encoding="utf-8")
    return index, False


def input_fingerprint(index: dict[str, Any]) -> str:
    """Fingerprint project-owned source/build inputs without generated artifacts."""
    fingerprint_entries = []
    for item in entries(index):
        record = {
            key: item.get(key)
            for key in ("relativePath", "lowerName", "suffix", "language", "ciYaml", "size")
        }
        if item.get("language"):
            if item.get("path"):
                try:
                    record["sha256"] = _sha256_file(Path(item["path"]))
                except OSError:
                    record["sha256"] = None
            source_size = item.get("size")
            source_modified_ns = item.get("modifiedNs")
            if item.get("path"):
                try:
                    current_stat = Path(str(item["path"])).stat()
                except OSError:
                    pass
                else:
                    source_size = current_stat.st_size
                    source_modified_ns = current_stat.st_mtime_ns
            record["size"] = source_size
            record["modifiedNs"] = source_modified_ns
        else:
            # Persistent COVER setup may restore identical build files with a
            # new timestamp. Content identity keeps resumable build checkpoints
            # stable while still invalidating real build-definition changes.
            content_sha256 = item.get("sha256")
            if item.get("path"):
                try:
                    content_sha256 = _sha256_file(Path(str(item["path"])))
                except OSError:
                    pass
            record["sha256"] = content_sha256
            if not content_sha256:
                record["modifiedNs"] = item.get("modifiedNs")
        fingerprint_entries.append(record)
    payload = {
        "schema": index.get("schema"),
        "projectRoot": index.get("projectRoot"),
        "entries": fingerprint_entries,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    value = build_index(args.project_root)
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
