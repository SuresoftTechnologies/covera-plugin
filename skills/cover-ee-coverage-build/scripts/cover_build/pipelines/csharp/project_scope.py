"""Resolve C# verification scope using solution mappings and MSBuild evaluation.

Project properties/items are read through a disposable "probe" wrapper project
(```<Import Project="App.csproj"/>``` plus a private target that dumps the
values to files) rather than the ``/getProperty``/``/getItem`` CLI switches,
which only exist on MSBuild 16.10+. The probe technique (Import + a custom
target using WriteLinesToFile) works on every MSBuild version the Toolbox
supports, so there is a single evaluation path instead of a version-gated one
with a less-accurate fallback. The probe file is created next to the real
project (not in a scratch directory) so ``$(MSBuildProjectDirectory)`` and the
Directory.Build.props/targets walk-up resolve exactly as they would for a
direct build of that project.
"""
from __future__ import annotations

import concurrent.futures
import re
import uuid
from pathlib import Path

from cover_build.common import processes
from cover_build.core.errors import ExecutorError

_MAX_WORKERS = 8
_TARGET_NAME = "_CoverScopeProbe"


def solution_roots(entry, projects, configuration, platform):
    if entry.suffix.lower() != '.sln':
        return [(p, configuration, platform) for p in projects]
    text = entry.read_text(encoding='utf-8-sig')
    configs = re.search(r'GlobalSection\(SolutionConfigurationPlatforms\).*?\n(.*?)EndGlobalSection', text, re.S)
    pairs = re.findall(r'^\s*([^=\r\n]+?)\s*=', configs.group(1) if configs else '', re.M)
    matches = [p.strip() for p in pairs if p.strip().split('|')[0] == configuration
               and (not platform or p.strip().split('|')[1].replace(' ', '').lower() == platform.replace(' ', '').lower())]
    if not platform:
        preferred = [p for p in matches if p.endswith('|Any CPU')]
        matches = preferred or matches
    if len(matches) != 1:
        raise ExecutorError('CSHARP_PROJECT_SCOPE_NOT_RESOLVED', 'Cannot resolve solution Configuration|Platform', [str(entry)])
    selected = matches[0]
    result = []
    for path, guid in re.findall(r'Project\("[^"]+"\)\s*=\s*"[^"]*",\s*"([^"]+\.csproj)",\s*"([^"]+)"', text, re.I):
        project = (entry.parent / path.replace('\\', '/')).resolve()
        if project not in projects:
            continue
        build = re.search(r'^\s*' + re.escape(guid + '.' + selected + '.Build.0') + r'\s*=\s*(.+)$', text, re.M | re.I)
        active = re.search(r'^\s*' + re.escape(guid + '.' + selected + '.ActiveCfg') + r'\s*=\s*(.+)$', text, re.M | re.I)
        if build and active:
            config, plat = active.group(1).strip().split('|', 1)
            result.append((project, config, plat))
    if not result:
        raise ExecutorError('CSHARP_PROJECT_SCOPE_NOT_RESOLVED', 'No enabled C# projects in selected solution configuration', [str(entry)])
    return result


def _scope_properties(specification):
    properties = []
    platform = None
    for arg in specification['arguments']:
        if re.match(r'[-/](?:p|property):', arg, re.I):
            properties.append(arg)
            match = re.search(r'(?:[:;])Platform=([^;]+)', arg, re.I)
            if match:
                platform = match.group(1).strip('"')
    args = specification['arguments']
    for index, arg in enumerate(args):
        if arg in ('--framework', '-f') and index + 1 < len(args):
            properties.append('/p:TargetFramework=' + args[index + 1])
        elif arg.startswith('--framework='):
            properties.append('/p:TargetFramework=' + arg.split('=', 1)[1])
    return properties, platform


def _identity(item):
    project, config, plat, framework = item
    return (str(project).casefold(), config, plat, framework)


def _escape_xml_attribute(value: str) -> str:
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _probe_project_xml(project_file_name: str) -> str:
    # WriteLinesToFile writes one line per item, so the field separator inside
    # a transformed line only has to avoid characters that are themselves
    # invalid in a Windows path; '|' qualifies.
    return (
        '<Project>\n'
        f'  <Import Project="{_escape_xml_attribute(project_file_name)}" />\n'
        f'  <Target Name="{_TARGET_NAME}">\n'
        '    <ItemGroup>\n'
        '      <_CoverScopeReference Include="@(ProjectReference)" />\n'
        '      <_CoverScopeProperty Include="ProjectAssetsFile=$(ProjectAssetsFile)" />\n'
        '      <_CoverScopeProperty Include="TargetFramework=$(TargetFramework)" />\n'
        '      <_CoverScopeFramework Include="$(TargetFrameworks)" />\n'
        '      <_CoverScopeProperty Include="RestoreProjectStyle=$(RestoreProjectStyle)" />\n'
        '    </ItemGroup>\n'
        '    <WriteLinesToFile File="$(_CoverScopePropertyOutput)" Lines="@(_CoverScopeProperty);@(_CoverScopeFramework-&gt;\'DeclaredFramework=%(Identity)\')" Overwrite="true" />\n'
        '    <WriteLinesToFile File="$(_CoverScopeReferenceOutput)" Overwrite="true"\n'
        '                       Lines="@(_CoverScopeReference-&gt;\'%(Identity)|%(FullPath)|%(BuildReference)|%(SetConfiguration)|%(SetPlatform)|%(SetTargetFramework)\')" />\n'
        '  </Target>\n'
        '</Project>\n'
    )


def _run_probe(project, config, plat, entry, properties, driver, specification, logs, index):
    """Evaluate one project's scope-relevant properties/items through a
    disposable probe project, without building it."""
    probe_path = project.parent / f'.cover-scope-probe-{uuid.uuid4().hex}.proj'
    try:
        probe_path.write_text(_probe_project_xml(project.name), encoding='utf-8')
    except OSError as exc:
        raise ExecutorError(
            'CSHARP_PROJECT_SCOPE_NOT_RESOLVED', f'Cannot write a scope probe project next to {project}', [str(probe_path)]
        ) from exc
    property_output = logs / f'csharp-project-scope-{index:03d}.properties.txt'
    reference_output = logs / f'csharp-project-scope-{index:03d}.references.txt'
    log_path = logs / f'csharp-project-scope-{index:03d}.log'
    try:
        args = (['msbuild'] if driver == 'CSHARP_DOTNET' else []) + [
            str(probe_path), f'/t:{_TARGET_NAME}', '/nologo', *properties,
            f'/p:Configuration={config}',
        ]
        if plat:
            args.append(f'/p:Platform={plat.replace(" ", "")}')
        if entry.suffix.lower() in {'.sln', '.slnx'}:
            args.append(f'/p:SolutionDir={entry.parent}/')
        args.append(f'/p:_CoverScopePropertyOutput={property_output}')
        args.append(f'/p:_CoverScopeReferenceOutput={reference_output}')
        spec = dict(specification, arguments=args)
        result = processes.run_command(spec, log_path)
    finally:
        probe_path.unlink(missing_ok=True)
    return result, property_output, reference_output


def _evaluate_batch(batch, entry, properties, driver, specification, logs, offset):
    """Evaluate independent projects in one BFS wave concurrently. Items in a
    wave don't depend on each other's result, only on the previous wave's
    discovered references, so this is safe to parallelize per wave."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(batch))) as executor:
        futures = [
            executor.submit(_run_probe, project, config, plat, entry,
                            properties + ([f'/p:TargetFramework={framework}'] if framework else []),
                            driver, specification, logs, offset + index)
            for index, (project, config, plat, framework) in enumerate(batch)
        ]
        return [future.result() for future in futures]


def _parse_probe_outputs(property_output: Path, reference_output: Path):
    properties = {}
    if property_output.is_file():
        for line in property_output.read_text(encoding='utf-8', errors='replace').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                if key == 'DeclaredFramework':
                    properties.setdefault('TargetFrameworks', []).append(value)
                else:
                    properties[key] = value
    references = []
    if reference_output.is_file():
        for line in reference_output.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line:
                continue
            fields = line.split('|')
            fields += [''] * (6 - len(fields))
            identity, full_path, build_reference, set_configuration, set_platform = fields[:5]
            references.append({
                'Identity': identity, 'FullPath': full_path, 'BuildReference': build_reference,
                'SetConfiguration': set_configuration, 'SetPlatform': set_platform,
                'SetTargetFramework': fields[5],
            })
    return properties, references


def _record_from_probe(project, config, plat, properties, references, log, solution_mapping):
    asset = properties.get('ProjectAssetsFile', '')
    required = properties.get('RestoreProjectStyle') == 'PackageReference' or bool(
        properties.get('TargetFramework') or properties.get('TargetFrameworks'))
    record = {
        'project': str(project), 'configuration': config, 'platform': plat,
        'targetFramework': properties.get('TargetFramework') or None,
        'assetsFile': str((project.parent / asset).resolve()) if asset else None,
        'requiresAssets': required, 'evidence': str(log),
    }
    discovered = []
    for ref in references:
        if (ref.get('BuildReference') or 'true').lower() == 'false':
            continue
        identity = ref.get('FullPath') or ref.get('Identity') or ''
        if not identity:
            continue
        path = (project.parent / identity).resolve()
        if path.suffix.lower() != '.csproj':
            continue
        ref_config, ref_platform = solution_mapping.get(path, (config, plat))
        for field, key in [('SetConfiguration', 'Configuration'), ('SetPlatform', 'Platform')]:
            value = ref.get(field) or ''
            if value.startswith(key + '='):
                if key == 'Configuration':
                    ref_config = value.split('=', 1)[1]
                else:
                    ref_platform = value.split('=', 1)[1]
        setting = ref.get('SetTargetFramework', '')
        discovered.append((path, ref_config, ref_platform,
                           setting.split('=', 1)[1] if setting.startswith('TargetFramework=') else None))
    return record, discovered


def resolve(entry, projects, configuration, specification, driver, logs):
    properties, platform = _scope_properties(specification)
    queue = solution_roots(entry, projects, configuration, platform)
    solution_mapping = {p: (c, t) for p, c, t in queue}
    seen = set()
    frontier = []
    for root in queue:
        item = (*root, None)
        identity = _identity(item)
        if identity not in seen:
            seen.add(identity)
            frontier.append(item)
    records = []
    counter = 0
    while frontier:
        batch = frontier
        frontier = []
        outcomes = _evaluate_batch(batch, entry, properties, driver, specification, logs, counter)
        counter += len(batch)
        for (project, config, plat, framework), (result, property_output, reference_output) in zip(batch, outcomes):
            if result['exitCode']:
                raise ExecutorError('CSHARP_PROJECT_SCOPE_NOT_RESOLVED', 'MSBuild project scope probe failed', [result['log']])
            properties_map, references = _parse_probe_outputs(property_output, reference_output)
            if not framework and not properties_map.get('TargetFramework') and properties_map.get('TargetFrameworks'):
                discovered = [(project, config, plat, tfm) for tfm in properties_map['TargetFrameworks']]
            else:
                record, discovered = _record_from_probe(project, config, plat, properties_map, references, result['log'], solution_mapping)
                records.append(record)
            for ref in discovered:
                identity = _identity(ref)
                if identity not in seen:
                    seen.add(identity)
                    frontier.append(ref)
    return records
