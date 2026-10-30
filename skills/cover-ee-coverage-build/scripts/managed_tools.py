from __future__ import annotations

"""Inspect installed JACOV and Java runtime artifacts without choosing a version."""


import argparse
import hashlib
import json
import sys
import zipfile
import re
from pathlib import Path


JACOV_SCHEMA_VERSION = "cover-jacov-install-evidence/v1"
EXPECTED_BIN_ARTIFACTS = (
    "cover-jacov-cli-jre8.jar",
    "cover-jacov-cli-jre17.jar",
    "jacov-maven-plugin-jre8.jar",
    "jacov-maven-plugin-jre17.jar",
    "jacov-gradle-plugin-jre11.jar",
    "jacov-gradle-plugin-jre17.jar",
    "jacov-gradle-init.gradle",
    "tm-javalib-lower.jar",
    "cover-javalib-agent.jar",
)
MAVEN_PROPERTIES = (
    "META-INF/maven/com.qualityscroll.cover/jacov-maven-plugin/pom.properties"
)
MAVEN_POM = "META-INF/maven/com.qualityscroll.cover/jacov-maven-plugin/pom.xml"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_properties(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _maven_plugin(path: Path) -> dict[str, str] | None:
    """Read the selected JACOV plugin's own Maven coordinate and embedded POM."""
    if not path.name.casefold().startswith("jacov-maven-plugin-"):
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(MAVEN_PROPERTIES) as stream:
                properties = _read_properties(stream.read().decode("utf-8"))
            if not all(properties.get(key) for key in ("groupId", "artifactId", "version")):
                return None
            if MAVEN_POM not in archive.namelist():
                return None
            return {
                "groupId": properties["groupId"],
                "artifactId": properties["artifactId"],
                "version": properties["version"],
                "pomPath": MAVEN_POM,
            }
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None


def inspect_jacov_install(install_root: Path) -> dict:
    install_root = install_root.resolve(strict=True)
    if not install_root.is_dir():
        raise NotADirectoryError(str(install_root))
    bin_dir = install_root / "bin"
    java_agent_dir = install_root / "java_agent"

    artifacts: list[dict] = []
    for name in EXPECTED_BIN_ARTIFACTS:
        path = bin_dir / name
        entry = {
            "name": name,
            "path": str(path),
            "exists": path.is_file(),
        }
        if path.is_file():
            entry["size"] = path.stat().st_size
            entry["sha256"] = _sha256(path)
            plugin = _maven_plugin(path)
            if plugin:
                entry["mavenPlugin"] = plugin
                # Keep this field for consumers of the v1 evidence schema.
                entry["mavenPluginVersion"] = plugin["version"]
            try:
                with zipfile.ZipFile(path) as archive:
                    descriptors = [n for n in archive.namelist() if n.startswith("META-INF/gradle-plugins/") and n.endswith(".properties")]
                    if name.startswith("jacov-gradle-plugin-") and len(descriptors) == 1:
                        properties = _read_properties(archive.read(descriptors[0]).decode("utf-8"))
                        implementation = properties.get("implementation-class", "")
                        header = archive.read(implementation.replace(".", "/") + ".class")[:8]
                        if header[:4] == b"\xca\xfe\xba\xbe":
                            entry["gradlePlugin"] = {"id": Path(descriptors[0]).stem, "implementationClass": implementation,
                                                     "minimumJavaMajor": int.from_bytes(header[6:8], "big") - 44}
                    variant = re.search(r"-jre(\d+)\.jar$", name)
                    if variant:
                        entry["javaVariant"] = int(variant.group(1))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                pass
        artifacts.append(entry)

    generated: list[dict] = []
    for name in ("cover-javalib-agent.jar", "tm-javalib-lower.jar"):
        path = java_agent_dir / name
        entry = {"name": name, "path": str(path), "exists": path.is_file()}
        if path.is_file():
            entry["size"] = path.stat().st_size
            entry["sha256"] = _sha256(path)
        generated.append(entry)

    return {
        "schemaVersion": JACOV_SCHEMA_VERSION,
        "installRoot": str(install_root),
        "binArtifacts": artifacts,
        "generatedJavaAgentArtifacts": generated,
        "selection": "MODEL_REQUIRED",
        "note": "Existence and versions are evidence only; select by the observed project JDK/source level.",
    }


def jacov_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the installed COVER JACOV tool set.")
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        result = inspect_jacov_install(args.install_root)
    except (FileNotFoundError, NotADirectoryError) as error:
        parser.error(f"invalid install root: {error}")

    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "jacov":
        raise SystemExit("usage: managed_tools.py jacov [arguments]")
    args.pop(0)
    return jacov_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
