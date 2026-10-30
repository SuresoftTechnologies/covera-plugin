"""Build against the target IDE's actual bundles; no Maven downloads needed."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

NAME = "com.qualityscroll.cover.collect"


def build(ide, jdk, *, output_directory=None, java_release=17):
    root = Path(__file__).resolve().parent
    manifest = (root / "META-INF/MANIFEST.MF").read_text(encoding="utf-8")
    if java_release != 17 and output_directory is None:
        raise ValueError("Compatibility builds require --output-directory; do not replace the verified distribution")
    if java_release not in (11, 17):
        raise ValueError("Supported compilation baselines: 11, 17")
    manifest = manifest.replace("Bundle-RequiredExecutionEnvironment: JavaSE-17", f"Bundle-RequiredExecutionEnvironment: JavaSE-{java_release}")
    version = next(line.split(":", 1)[1].strip() for line in manifest.splitlines() if line.startswith("Bundle-Version:"))
    ide, jdk = Path(ide).resolve(strict=True), Path(jdk).resolve(strict=True)
    plugins = ide / "plugins"
    jars = sorted(plugins.glob("*.jar"))
    if not jars:
        raise ValueError("IDE plugins directory has no bundles")
    javac = jdk / "bin" / ("javac.exe" if (jdk / "bin/javac.exe").exists() else "javac")
    import os
    target = Path(output_directory).resolve() if output_directory else root.parent / "assets/plugin"
    if java_release != 17 and target == (root.parent / "assets/plugin").resolve():
        raise ValueError("Compatibility candidates may not replace the verified distribution")
    target.mkdir(parents=True, exist_ok=True)
    jar = target / f"{NAME}_{version}.jar"
    source_paths = sorted((root / "src").rglob("*.java"))
    with tempfile.TemporaryDirectory(prefix="cover-cdt-build-") as directory:
        classes = Path(directory) / "classes"
        classes.mkdir()
        arguments = ["--release", str(java_release), "-encoding", "UTF-8", "-classpath", os.pathsep.join(map(str, jars)),
                     "-d", str(classes), *map(str, source_paths)]
        # javac @file avoids Windows command-line length limits.
        argfile = Path(directory) / "javac.args"
        argfile.write_text("\n".join('"'+a.replace("\\", "/").replace('"', '\\"')+'"' for a in arguments), encoding="utf-8")
        subprocess.run([str(javac), "@"+str(argfile)], check=True)
        with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            entries = {"META-INF/MANIFEST.MF": manifest.replace("\r\n", "\n").replace("\n", "\r\n").encode(),
                       "plugin.xml": (root / "plugin.xml").read_bytes()}
            entries.update({p.relative_to(classes).as_posix(): p.read_bytes() for p in classes.rglob("*.class")})
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
    sources = [*source_paths, root / "META-INF/MANIFEST.MF", root / "plugin.xml", root / "build_plugin.py", root / "build.properties"]
    try:
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = None
    import re
    ranges = {name: {"min": low, "maxExclusive": high} for name, low, high in
              re.findall(r'([\w.]+);bundle-version="\[([^,]+),([^\)]+)\)"', manifest)}
    report = {"pluginId": NAME, "version": version, "protocol": 1, "jar": jar.name,
              "sha256": hashlib.sha256(jar.read_bytes()).hexdigest(), "sourceCommit": commit,
              "sourceHashes": {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
              "javaRelease": java_release, "compatibleBundles": ranges,
              "validation": "COMPILE_ONLY",
              "effectiveManifestSha256": hashlib.sha256(manifest.replace("\r\n", "\n").replace("\n", "\r\n").encode()).hexdigest(),
              "compiledAgainst": [p.name for p in jars if p.name.startswith(("org.eclipse.cdt.dsf_", "org.eclipse.cdt.dsf.gdb_", "org.eclipse.core.runtime_", "com.google.gson_"))]}
    (target / "release.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ide-home", required=True)
    parser.add_argument("--jdk-home", required=True)
    parser.add_argument("--java-release", type=int, choices=(11, 17), default=17)
    parser.add_argument("--output-directory", help="Write a compatibility candidate without replacing the bundled JAR")
    args = parser.parse_args()
    print(json.dumps(build(args.ide_home, args.jdk_home, output_directory=args.output_directory, java_release=args.java_release), indent=2))
