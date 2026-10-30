"""Read the selected IDE's registered bundles and bundled Java without starting it."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import zipfile
import xml.etree.ElementTree as ET


BUNDLES = (
    "org.eclipse.platform", "org.eclipse.core.runtime", "org.eclipse.core.resources",
    "org.eclipse.ui", "org.eclipse.debug.core", "org.eclipse.cdt.core",
    "org.eclipse.cdt.debug.core", "org.eclipse.cdt.dsf", "org.eclipse.cdt.dsf.gdb",
    "com.google.gson", "org.eclipse.osgi",
)


def manifest(path):
    if path.is_dir():
        text = (path / "META-INF/MANIFEST.MF").read_text(encoding="utf-8")
    else:
        with zipfile.ZipFile(path) as archive:
            text = archive.read("META-INF/MANIFEST.MF").decode("utf-8")
    unfolded = re.sub(r"\r?\n ", "", text)
    return dict(line.split(": ", 1) for line in unfolded.splitlines() if ": " in line)


def location_path(ide, location):
    if location.startswith("file:"):
        parsed = urlparse(location)
        if parsed.netloc:
            raise ValueError("Remote bundle location is not inspected")
        location = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:", location):
            location = location[1:]
    path = Path(location)
    return path if path.is_absolute() else ide / path


def inspect(ide_home):
    ide = Path(ide_home).resolve(strict=True)
    ini = ide / "stm32cubeide.ini"
    lines = ini.read_text(encoding="utf-8-sig").splitlines() if ini.is_file() else []
    build = [line.split("=", 1)[1] for line in lines if line.startswith("-Declipse.buildId=")]
    result = {"schema": "cover-cdt-installation/v1", "ide": str(ide),
              "ideBuildId": build[0] if len(build) == 1 else None,
              "java": None, "features": {}, "bundles": {}, "issues": [],
              "validation": {"installation": "NOT_TESTED", "startup": "NOT_TESTED",
                             "debugSession": "NOT_TESTED", "memoryCapture": "NOT_TESTED"}}
    for name in ("org.eclipse.cdt", "org.eclipse.platform"):
        entries = []
        for path in sorted((ide / "features").glob(name + "_*")):
            try:
                if path.is_dir():
                    feature = ET.parse(path / "feature.xml").getroot()
                else:
                    with zipfile.ZipFile(path) as archive:
                        feature = ET.fromstring(archive.read("feature.xml"))
                entries.append({"id": feature.get("id"), "version": feature.get("version")})
            except (OSError, ET.ParseError, KeyError, zipfile.BadZipFile):
                result["issues"].append("FEATURE_UNREADABLE: " + name)
        result["features"][name] = entries
    # A stale requiredJavaVersion=1.8 in the launcher is not the runtime version.
    try:
        vm = location_path(ide, lines[lines.index("-vm") + 1].strip())
        start = vm if vm.is_dir() else vm.parent
        for parent in (start, *list(start.parents)[:3]):
            release = parent / "release"
            if release.is_file():
                values = dict(line.split("=", 1) for line in release.read_text(encoding="utf-8").splitlines() if "=" in line)
                result["java"] = {"vm": str(vm), "releaseFile": str(release),
                                  "version": values.get("JAVA_VERSION", "").strip('"'),
                                  "implementor": values.get("IMPLEMENTOR", "").strip('"')}
                break
        if not result["java"]:
            result["issues"].append("JAVA_RELEASE_NOT_FOUND")
    except (ValueError, IndexError, OSError) as error:
        result["issues"].append("JAVA_CONFIG_UNRESOLVED: " + str(error))

    info = ide / "configuration/org.eclipse.equinox.simpleconfigurator/bundles.info"
    if not info.is_file():
        result["issues"].append("BUNDLE_REGISTRY_NOT_FOUND")
        return result
    for line in info.read_text(encoding="utf-8-sig").splitlines():
        fields = line.split(",")
        if len(fields) < 3 or fields[0] not in BUNDLES:
            continue
        name, version, location = fields[:3]
        entry = {"registeredVersion": version, "location": location}
        try:
            path = location_path(ide, location)
            data = manifest(path)
            entry.update(version=data.get("Bundle-Version"),
                         javaRequirement=data.get("Bundle-RequiredExecutionEnvironment"),
                         requireBundle=data.get("Require-Bundle"))
            if data.get("Bundle-SymbolicName", "").split(";")[0] != name or entry["version"] != version:
                result["issues"].append("BUNDLE_REGISTRY_MISMATCH: " + name)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            result["issues"].append("BUNDLE_UNREADABLE: " + name + ": " + str(error))
        result["bundles"].setdefault(name, []).append(entry)
    for name in BUNDLES:
        if len(result["bundles"].get(name, [])) != 1:
            result["issues"].append("BUNDLE_NOT_UNIQUE: " + name)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ide-home", action="append", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps([inspect(path) for path in args.ide_home], indent=2, ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, f"IDE inspection failed: {error}\n")
