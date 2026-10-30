"""Install the bundled CDT plugin without stopping or starting the IDE."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import re
import collect_environment as environment
from inspect_cdt_compatibility import inspect as inspect_components
from urllib.parse import unquote, urlparse

PLUGIN_ID = "com.qualityscroll.cover.collect"
ASSETS = Path(__file__).resolve().parents[1] / "assets/plugin"


def compatibility(ide, release):
    ranges = release.get("compatibleBundles")
    if not ranges:
        raise ValueError("Release has no compatibility policy; rebuild the distribution")
    found = {}
    def number(version):
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\.|$)", version)
        if not match: raise ValueError("Invalid OSGi version: " + version)
        return tuple(map(int, match.groups()))
    for name, bounds in ranges.items():
        matches = []
        for jar in (Path(ide) / "plugins").glob(name + "_*.jar"):
            version = jar.name[len(name)+1:-4]
            if number(bounds["min"]) <= number(version) < number(bounds["maxExclusive"]): matches.append(jar.name)
        if len(matches) != 1:
            raise ValueError(f"IDE_BUNDLE_UNSUPPORTED: {name}; expected {bounds}")
        found[name] = matches[0]
    return found


def installed_bundles(ide):
    info = Path(ide) / "configuration/org.eclipse.equinox.simpleconfigurator/bundles.info"
    result = []
    if info.is_file():
        for line in info.read_text(encoding="utf-8").splitlines():
            if not line.startswith(PLUGIN_ID + ","): continue
            fields = line.split(",")
            version = fields[1]
            if not re.fullmatch(r"\d+\.\d+\.\d+", version): raise ValueError("Unsupported collector version")
            result.append(version)
    return result


def registered_files(ide):
    ide = Path(ide).resolve(strict=True)
    info = ide / "configuration/org.eclipse.equinox.simpleconfigurator/bundles.info"
    result = {}
    if not info.is_file(): return result
    for line in info.read_text(encoding="utf-8").splitlines():
        if not line.startswith(PLUGIN_ID + ","): continue
        fields = line.split(",")
        location = fields[2]
        if location.startswith("file:"):
            parsed = urlparse(location)
            if parsed.netloc: raise ValueError("Remote collector bundle location is unsupported")
            location = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", location): location = location[1:]
        path = Path(location)
        if not path.is_absolute(): path = ide / path
        path = path.resolve(strict=True)
        if not path.is_relative_to(ide) or not path.is_file(): raise ValueError("Registered bundle escapes selected IDE")
        result[fields[1]] = path
    return result


def eclipse_command(ide):
    """Use the IDE's own Java, not a possibly different system JDK."""
    lines = (ide / "stm32cubeide.ini").read_text(encoding="utf-8-sig").splitlines()
    try:
        vm = Path(lines[lines.index("-vm")+1].strip())
    except (ValueError, IndexError) as error:
        raise ValueError("IDE Java path (-vm) must be confirmed before installation") from error
    if not vm.is_absolute(): vm = ide / vm
    if vm.is_dir(): vm = vm / ("java.exe" if os.name == "nt" else "java")
    if vm.name.lower() == "javaw.exe": vm = vm.with_name("java.exe")
    launchers = list((ide / "plugins").glob("org.eclipse.equinox.launcher_*.jar"))
    if not vm.is_file() or len(launchers) != 1:
        raise ValueError("IDE Java or Eclipse launcher could not be uniquely located")
    return [str(vm), "-jar", str(launchers[0]), "-install", str(ide),
            "-configuration", str(ide / "configuration"), "-nosplash", "-consoleLog"]


def register_plugin(ide_home, version, *, action="install", restore_from=None):
    """Install one local bundle using p2; do not rely on dropins auto-start."""
    ide = Path(ide_home).resolve(strict=True)
    assets = Path(__file__).resolve().parents[1] / "assets/plugin"
    release = json.loads((assets / "release.json").read_text(encoding="utf-8"))
    if version != release["version"]: raise ValueError("Plugin version mismatch")
    existing = installed_bundles(ide)
    previous_files = registered_files(ide)
    if len(existing) > 1: raise ValueError("Multiple collector registrations require manual review")
    target_jar = assets / release["jar"]
    target_version = version
    if action == "rollback":
        if not restore_from: raise ValueError("--restore-from is required")
        backup = json.loads((Path(restore_from) / "installation.json").read_text(encoding="utf-8"))
        if backup["ide"] != str(ide) or len(backup["previousVersions"]) != 1:
            raise ValueError("Rollback backup does not match this IDE or has no previous version")
        target_version = backup["previousVersions"][0]
        target_jar = Path(restore_from) / "source/plugins" / f"{PLUGIN_ID}_{target_version}.jar"
        known = json.loads((assets / "known-releases.json").read_text(encoding="utf-8"))
        known[version] = release["sha256"]
        if not target_jar.is_file() or hashlib.sha256(target_jar.read_bytes()).hexdigest() != known.get(target_version):
            raise ValueError("Rollback bundle checksum is not a known release")
    info = ide / "configuration/org.eclipse.equinox.simpleconfigurator/bundles.info"
    installed_jar = ide / "plugins" / release["jar"]
    def registered():
        return (info.is_file() and any(line.startswith(f"com.qualityscroll.cover.collect,{version},")
                for line in info.read_text(encoding="utf-8").splitlines()) and installed_jar.is_file()
                and hashlib.sha256(installed_jar.read_bytes()).hexdigest() == release["sha256"])
    if action == "install" and registered(): return {"registrationVerified": True, "registrationLog": None, "registrationReused": True}
    if action == "install" and existing: raise ValueError("Use --action update for an existing different collector")
    config = (ide / "configuration/config.ini").read_text(encoding="utf-8").splitlines()
    profiles = [line.split("=", 1)[1].strip() for line in config if line.startswith("eclipse.p2.profile=")]
    if len(profiles) != 1 or not profiles[0]: raise ValueError("IDE p2 profile is missing")
    base = eclipse_command(ide)
    work = Path(tempfile.mkdtemp(prefix="cover-cdt-register-"))
    log = work / "registration.log"
    source = work / "source"
    (source / "plugins").mkdir(parents=True)
    old_source = work / "backup/source/plugins"
    old_source.mkdir(parents=True)
    for old in existing:
        shutil.copyfile(previous_files[old], old_source / f"{PLUGIN_ID}_{old}.jar")
    (work / "backup/installation.json").write_text(json.dumps({"ide": str(ide), "previousVersions": existing,
        "action": action, "targetVersion": target_version}, indent=2), encoding="utf-8")
    shutil.copyfile(info, work / "backup/bundles.info")
    shutil.copyfile(target_jar, source / "plugins" / target_jar.name)
    # Publish the previous known bundle too: p2 must resolve an old IU before
    # it can uninstall it during an update or remove operation.
    for old in existing:
        shutil.copyfile(previous_files[old], source / "plugins" / f"{PLUGIN_ID}_{old}.jar")
    repository = (work / "repository").as_uri()
    publisher = base + ["-application", "org.eclipse.equinox.p2.publisher.FeaturesAndBundlesPublisher",
                       "-metadataRepository", repository, "-artifactRepository", repository,
                       "-source", str(source), "-publishArtifacts", "-compress"]
    director = base + ["-application", "org.eclipse.equinox.p2.director", "-repository", repository,
                      "-destination", str(ide),
                      "-profile", profiles[0]]
    if action != "remove": director += ["-installIU", f"{PLUGIN_ID}/{target_version}"]
    # The previous known IU is published above so p2 can remove it as part of
    # the same transaction before installing the replacement.
    if existing:
        director += ["-uninstallIU", ",".join(f"{PLUGIN_ID}/{old}" for old in existing)]
    if action == "remove" and not existing: raise ValueError("Collector is not registered")
    try:
        with log.open("wb") as output:
            for command in (publisher, director + ["-verifyOnly"], director + ["-tag", f"COVER-collector-{version}"]):
                process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=120, check=False,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if process.returncode != 0:
                    raise ValueError(f"Plugin registration failed ({process.returncode}); see {log}")
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"Plugin registration timed out; inspect {log} before retrying") from error
    current = installed_bundles(ide)
    verified = not current if action == "remove" else (current == [target_version] and
        (ide / "plugins" / target_jar.name).is_file() and
        hashlib.sha256((ide / "plugins" / target_jar.name).read_bytes()).hexdigest() == hashlib.sha256(target_jar.read_bytes()).hexdigest())
    if not verified:
        raise ValueError(f"Eclipse did not register the collector; see {log}")
    return {"registrationVerified": True, "registrationLog": str(log), "backupDirectory": str(work / "backup"), "action": action}


def manage(ide_home, action="install", *, ide_closed=False, restore_from=None):
    ide = Path(ide_home).resolve(strict=True)
    release = json.loads((ASSETS / "release.json").read_text(encoding="utf-8"))
    current = installed_bundles(ide)
    if action == "status":
        result = {"ide": str(ide), "registeredVersions": current, "distributionVersion": release["version"],
                  "components": inspect_components(ide)}
        try:
            result["matchingBundles"] = compatibility(ide, release)
            result["bundleRangeMatched"] = True
        except ValueError as error:
            result["bundleRangeMatched"] = False
            result["compatibilityReason"] = str(error)
        return result
    if not ide_closed: raise ValueError("Close the IDE after preserving test data, then pass --ide-closed")
    environment.require_closed(ide)
    if action not in ("remove", "rollback"): compatibility(ide, release)
    known = json.loads((ASSETS / "known-releases.json").read_text(encoding="utf-8"))
    known[release["version"]] = release["sha256"]
    files = list((ide / "plugins").glob(PLUGIN_ID + "_*.jar"))
    registered = registered_files(ide)
    for version, path in registered.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != known.get(version):
            raise ValueError("Registered collector is not a known release: " + str(path))
    dropins = list((ide / "dropins").glob("**/" + PLUGIN_ID + "_*.jar"))
    for jar in files + dropins:
        if jar.is_symlink() or not jar.resolve().is_relative_to(ide): raise ValueError("Collector path escapes IDE")
        version = jar.name[len(PLUGIN_ID)+1:-4]
        if hashlib.sha256(jar.read_bytes()).hexdigest() != known.get(version):
            raise ValueError("Modified or unknown collector must be reviewed: " + str(jar))
    if action == "install" and current and current != [release["version"]]:
        raise ValueError("Use --action update; existing version will be backed up")
    packaged = ASSETS / release["jar"]
    if hashlib.sha256(packaged.read_bytes()).hexdigest() != release["sha256"]:
        raise ValueError("Bundled plugin checksum mismatch")
    # Retire only known dropins copies; otherwise they can resurrect a removed IU.
    migration = Path(tempfile.mkdtemp(prefix="cover-cdt-dropins-backup-"))
    moved = []
    for index, jar in enumerate(dropins):
        target = migration / f"{index}-{jar.name}"
        shutil.copyfile(jar, target)
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(jar.read_bytes()).digest():
            raise ValueError("Dropins backup verification failed")
        moved.append({"original": str(jar), "backup": str(target)})
    (migration / "files.json").write_text(json.dumps(moved, indent=2), encoding="utf-8")
    try:
        result = register_plugin(ide, release["version"], action=action, restore_from=restore_from)
    except Exception as error:
        raise ValueError(f"Installation incomplete: {error}; dropins backups: {migration}. Inspect p2 state before rollback.") from error
    for item in moved:
        original = Path(item["original"])
        if original.exists():
            if hashlib.sha256(original.read_bytes()).digest() != hashlib.sha256(Path(item["backup"]).read_bytes()).digest():
                raise ValueError("Dropins changed during registration; preserved: " + str(original))
            original.unlink()
    result.update(status="REMOVED" if action == "remove" else "INSTALLED_RESTART_REQUIRED", dropinsBackup=str(migration))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ide-home", required=True)
    parser.add_argument("--ide-closed", action="store_true")
    parser.add_argument("--action", choices=("status", "install", "update", "remove", "rollback"), default="install")
    parser.add_argument("--restore-from")
    args = parser.parse_args()
    try:
        result = manage(args.ide_home, args.action, ide_closed=args.ide_closed, restore_from=args.restore_from)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"COVER plugin installation failed: {error}\n")
