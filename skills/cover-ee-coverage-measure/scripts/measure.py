"""Read-only module coverage query; shares covera credentials and curl transport."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'cover-ee-coverage-build' / 'scripts'))
from cover_build.common import open_api_credentials, servers
from cover_build.common.open_api import _curl_request
from cover_build.core.errors import ExecutorError

BASE = '/ai-agent-kit/module'


def require(condition, code='INVALID_RESPONSE', message='Unexpected COVER response.'):
    if not condition:
        raise ExecutorError(code, message)


def module_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{24}', value),
            'INVALID_MODULE_ID', 'Use the build module ID, not a symbolicmoduleid URL.')
    return value.lower()


def origin(value):
    parsed = urlsplit(value)
    require(parsed.username is None and parsed.password is None, 'INVALID_SERVER_URL', 'Credentials are not allowed in URLs.')
    try:
        result = servers.open_api_server_origin(value)
        parsed = urlsplit(result)
        return result, (parsed.hostname.lower(), parsed.port or 443, parsed.path)
    except (ValueError, ExecutorError):
        raise ExecutorError('INVALID_SERVER_URL', 'Provide a valid HTTPS main web server URL.') from None


def connection(args):
    install = os.environ.get('CODESCROLL_TM_INSTALL_PATH')
    cached = open_api_credentials.load(open_api_credentials.credentials_path(install)) if install else None
    raw_url = args.server_url or (cached or {}).get('openApiServerUrl')
    require(bool(raw_url), 'SERVER_URL_REQUIRED', 'Provide the COVER main web server URL.')
    server, identity = origin(raw_url)
    key = sys.stdin.readline().strip() if args.access_key_stdin else None
    if not args.access_key_stdin and cached and origin(cached['openApiServerUrl'])[1] == identity:
        key = cached['accessKey']
    require(bool(key), 'ACCESS_KEY_REQUIRED', 'Provide a key for this server using --access-key-stdin.')
    require(not any(c in key for c in '\r\n"\\') and all(ord(c) >= 32 for c in key),
            'INVALID_ACCESS_KEY', 'Invalid Access Key format.')
    return server, key


class Client:
    def __init__(self, server, key, verify_tls=False):
        self.server = origin(server)[0]
        self.key = key
        self.verify_tls = verify_tls

    def get(self, route, query=None):
        require(route == BASE + '/search' or re.fullmatch(BASE + r'/[0-9a-f]{24}(?:/coverage)?', route),
                'INVALID_ROUTE', 'Only module lookup GET requests are allowed.')
        url = self.server + '/cover-open-api' + route
        if query:
            url += '?' + urlencode(query)
        status, body, _ = _curl_request(url, method='GET', timeout=30,
                                      insecure_skip_tls_verify=not self.verify_tls,
                                      token=self.key, memory_response=True)
        # Never expose echoed keys or arbitrary server error messages.
        require(self.key.encode() not in body, 'SECRET_IN_RESPONSE', 'Server echoed a credential; response withheld.')
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            value = None
        if status != 200:
            code = value.get('code') if isinstance(value, dict) else None
            code = code if status == 409 and code in ('CURSOR_STALE', 'COVERAGE_NOT_READY') else 'HTTP_' + str(status)
            raise ExecutorError(code, 'COVER lookup failed; check authentication, access, target and API availability.')
        require(isinstance(value, dict))
        return value


def coverage_counts(value):
    require(isinstance(value, dict))
    result = {}
    for kind, counts in value.items():
        if counts is None:
            result[kind] = None
            continue
        require(isinstance(counts, dict))
        covered, total = counts.get('covered'), counts.get('total')
        require(type(covered) is int and type(total) is int and 0 <= covered <= total and total > 0)
        result[kind] = {'covered': covered, 'total': total, 'percent': round(100 * covered / total, 2)}
    return result


def metadata(client, selected):
    value = client.get(BASE + '/' + selected)
    require(value.get('moduleId') == selected and isinstance(value.get('name'), str))
    return {key: value.get(key) for key in ('moduleId', 'name', 'registeredAt', 'lastMeasuredAt', 'testingStandard')}


def collect(client, selected, *, details=False, limit=100, max_pages=1000):
    selected = module_id(selected)
    require(1 <= limit <= 2000 and max_pages > 0, 'INVALID_LIMIT', 'Invalid pagination limits.')
    info = metadata(client, selected)
    started = datetime.now(timezone.utc).isoformat()
    cursor, summary = None, None
    seen_cursors, seen_files, files = set(), set(), []
    for _ in range(max_pages):
        query = {'limit': limit if details else 1}
        if cursor is not None:
            query['cursor'] = cursor
        page = client.get(BASE + '/' + selected + '/coverage', query)
        require(page.get('moduleId') == selected, 'MODULE_CHANGED', 'Module changed during lookup.')
        require(isinstance(page.get('summary'), dict) and isinstance(page.get('files'), list) and 'nextCursor' in page)
        current = page['summary']
        coverage_counts(current.get('coverage'))
        require(summary is None or summary == current, 'DATA_CHANGED', 'Coverage changed during lookup. Restart collection.')
        summary = current
        if not details:
            break
        for row in page['files']:
            require(isinstance(row, dict) and isinstance(row.get('fileId'), str) and isinstance(row.get('path'), str))
            require(row['fileId'] not in seen_files, 'UNSTABLE_PAGING', 'Duplicate file across pages.')
            seen_files.add(row['fileId'])
            coverage_counts(row.get('coverage'))
            require(isinstance(row.get('functions'), list))
            functions = []
            for func in row['functions']:
                require(isinstance(func, dict) and isinstance(func.get('functionId'), str)
                        and isinstance(func.get('signature'), str) and type(func.get('executed')) is bool)
                coverage_counts(func.get('coverage'))
                functions.append({k: func.get(k) for k in ('functionId', 'signature', 'executed', 'coverage', 'complexity')})
            files.append({**{k: row.get(k) for k in ('fileId', 'path', 'coverage', 'complexity')}, 'functions': functions})
        cursor = page['nextCursor']
        if cursor is None:
            break
        require(isinstance(cursor, str) and cursor and cursor not in seen_cursors,
                'UNSTABLE_PAGING', 'Invalid or repeated cursor.')
        seen_cursors.add(cursor)
    else:
        raise ExecutorError('PAGE_LIMIT', 'Incomplete collection; no partial result was published.')
    require(metadata(client, selected) == info, 'DATA_CHANGED', 'Module metadata changed during lookup.')
    result = {'schema': 'cover-measure/v1', 'status': 'SUCCEEDED', 'serverUrl': client.server,
              'scope': 'LATEST_MERGED_COVERAGE', 'startedAt': started,
              'queriedAt': datetime.now(timezone.utc).isoformat(), 'module': info,
              'summary': {k: summary.get(k) for k in ('fileCount', 'functionCount', 'lineCount', 'complexity')},
              'coverage': coverage_counts(summary['coverage']), 'detailsIncluded': details}
    if details:
        result['files'] = files
    return result


def run(args, client):
    candidate = None
    if args.module_name:
        require(args.module_name.strip() and not args.module_name.endswith(('/', '\\')),
                'INVALID_MODULE_NAME', 'Enter a full filename or path suffix.')
        response = client.get(BASE + '/search', {'name': args.module_name.strip()})
        candidates = response.get('modules')
        require(isinstance(candidates, list))
        safe = []
        for item in candidates:
            require(isinstance(item, dict) and isinstance(item.get('buildOutputPath'), str))
            module_id(item.get('moduleId'))
            safe.append({k: item.get(k) for k in ('moduleId', 'name', 'buildOutputPath', 'agentName', 'macAddress', 'registeredAt')})
        if len(safe) != 1:
            return {'status': 'WAITING_USER' if safe else 'NOT_FOUND', 'candidates': safe}
        candidate = safe[0]
        selected = candidate['moduleId']
    else:
        selected = args.module_id
    result = collect(client, selected, details=args.details, limit=args.limit, max_pages=args.max_pages)
    if candidate:
        result['selectedCandidate'] = candidate
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-url')
    parser.add_argument('--access-key-stdin', action='store_true')
    parser.add_argument('--verify-tls', action='store_true')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--module-name')
    target.add_argument('--module-id')
    parser.add_argument('--details', action='store_true')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--max-pages', type=int, default=1000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        server, key = connection(args)
        result = run(args, Client(server, key, args.verify_tls))
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            result = {k: v for k, v in result.items() if k != 'files'}
            result['output'] = str(args.output.absolute())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'SUCCEEDED' else 4
    except ExecutorError as exc:
        # Messages/evidence from shared helpers may include untrusted URLs; return only the code.
        print(json.dumps({'status': 'FAILED', 'code': exc.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({'status': 'FAILED', 'code': 'LOCAL_INPUT_OR_OUTPUT_ERROR'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
