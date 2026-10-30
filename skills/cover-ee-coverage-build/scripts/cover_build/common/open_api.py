"""Open API v1 contract validation and curl-based requests."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import jobs
from cover_build.common import pathutil
from cover_build.common import servers
from cover_build.core import errors


def load_contract(path: str | Path) -> dict[str, Any]:
    contract = fileio.load_json(path)
    if contract.get("contract") != "cover-open-api/v1":
        raise errors.ExecutorError("UNSUPPORTED_API_CONTRACT", "Expected cover-open-api/v1")
    return contract


def parse_key_values(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise errors.ExecutorError("INVALID_QUERY_PARAMETER", f"Expected KEY=VALUE: {value}")
        key, item = value.split("=", 1)
        result[key] = item
    return result


def validate_payload(operation: dict[str, Any], payload: Any) -> None:
    schema = operation.get("request", {})
    _validate_api_value(schema, payload, "request")
    if operation.get("operationId") == "updateAgent":
        for index, item in enumerate(payload.get("toolChains", [])):
            compiler = item["toolKind"] in {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}
            if compiler != ("toolChainInfoId" in item):
                rule = "required for compilers" if compiler else "not allowed for linkers or archivers"
                raise errors.ExecutorError("INVALID_API_PAYLOAD", f"request.toolChains[{index}].toolChainInfoId is {rule}")


def _validate_api_value(schema: dict[str, Any], payload: Any, location: str) -> None:
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(payload, dict),
        "array": isinstance(payload, list),
        "string": isinstance(payload, str),
        "boolean": isinstance(payload, bool),
        "integer": isinstance(payload, int) and not isinstance(payload, bool),
    }
    if expected in valid_type and not valid_type[expected]:
        raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} must be {expected}")
    if expected != "object":
        if schema.get("enum") and payload not in schema["enum"]:
            raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} is not an allowed value")
        if expected == "integer" and payload < schema.get("minimum", payload):
            raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} is below minimum")
        if expected == "string":
            if len(payload.strip()) < schema.get("minLength", 0):
                raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} is too short")
            if schema.get("pattern") and not re.fullmatch(schema["pattern"], payload):
                raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} has invalid format")
            if schema.get("format") == "parserAlias" and (
                not 1 <= len(payload) <= 100 or payload != payload.strip() or ".." in payload
                or re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', payload)
            ):
                raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} must be a safe parser file name")
            if schema.get("format") == "parserLine" and (
                "\r" in payload or "\n" in payload or (
                    payload.strip() and not payload.lstrip().startswith("#")
                    and not re.fullmatch(r"\S+(=.*)?", payload)
                )
            ):
                raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} must be one parser configuration line")
        if expected == "array":
            if len(payload) > schema.get("maximumItems", len(payload)):
                raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} contains too many items")
            item_schema = schema.get("items") or ({"type": schema["itemType"]} if schema.get("itemType") else {})
            for index, item in enumerate(payload):
                _validate_api_value(item_schema, item, f"{location}[{index}]")
        return
    if schema.get("type") == "object" and not isinstance(payload, dict):
        raise errors.ExecutorError("INVALID_API_PAYLOAD", "Request payload must be an object")
    for key in schema.get("required", []):
        if key not in payload:
            raise errors.ExecutorError("INVALID_API_PAYLOAD", f"Missing required property: {key}")
    properties = schema.get("properties", {})
    if len(payload) < schema.get("minProperties", 0):
        raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} must not be empty")
    if schema.get("additionalProperties") is False and set(payload) - set(properties):
        raise errors.ExecutorError("INVALID_API_PAYLOAD", f"{location} contains undeclared properties")
    for key, rule in properties.items():
        if key not in payload:
            continue
        _validate_api_value(rule, payload[key], f"{location}.{key}")


def make_ssl_context(ca_file: str | None, insecure_skip_tls_verify: bool = False) -> ssl.SSLContext:
    if ca_file and insecure_skip_tls_verify:
        raise errors.ExecutorError(
            "TLS_CONFIGURATION_CONFLICT",
            "A CA file cannot be combined with disabled TLS verification",
        )
    context = ssl.create_default_context(cafile=str(pathutil.canonical(ca_file)) if ca_file else None)
    if insecure_skip_tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def validate_insecure_tls_request(args: argparse.Namespace) -> None:
    if not bool(getattr(args, "insecure_skip_tls_verify", False)):
        return
    if not getattr(args, "job_context", None):
        raise errors.ExecutorError(
            "INSECURE_TLS_JOB_CONTEXT_REQUIRED",
            "Disabled TLS verification requires an integrity-protected COVER job context",
        )
    job = jobs.load_job_context(args.job_context)
    if job.get("insecureSkipTlsVerify") is not True:
        raise errors.ExecutorError(
            "INSECURE_TLS_NOT_AUTHORIZED_FOR_JOB",
            "This COVER job was not created with TLS verification disabled",
        )
    if servers.server_origin(args.base_url).casefold() != str(job["serverOrigin"]).casefold():
        raise errors.ExecutorError(
            "INSECURE_TLS_SERVER_MISMATCH",
            "The API base URL does not match the server origin protected by the COVER job",
        )


def _curl_request(
    url: str,
    *,
    method: str,
    timeout: int,
    insecure_skip_tls_verify: bool,
    ca_file: str | None = None,
    token: str | None = None,
    data: bytes | None = None,
    memory_response: bool = False,
) -> tuple[int, bytes, str]:
    """Call a COVER HTTPS endpoint without exposing the Access Key in argv."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise errors.ExecutorError("COVER_HTTPS_REQUIRED", "Runner-owned COVER requests must use HTTPS")
    curl = shutil.which("curl.exe" if os.name == "nt" else "curl") or shutil.which("curl")
    if not curl:
        raise errors.ExecutorError("CURL_NOT_FOUND", "curl is required for COVER Open API requests")
    if ca_file and insecure_skip_tls_verify:
        raise errors.ExecutorError("TLS_CONFIGURATION_CONFLICT", "A CA file cannot be combined with disabled TLS verification")

    with tempfile.TemporaryDirectory(prefix="cover-api-") as directory:
        temporary = Path(directory)
        response_path = temporary / "response.bin"
        arguments = [
            curl,
            "--silent",
            "--show-error",
            "--proto",
            "=https",
            "--request",
            method,
            "--url",
            url,
            "--header",
            "Accept: application/json",
            "--header",
            "Content-Type: application/json",
            "--output",
            str(response_path),
            "--write-out",
            "%{http_code}\n%{content_type}",
            "--max-time",
            str(timeout),
        ]
        standard_input = None
        if memory_response:
            offset = arguments.index("--output")
            del arguments[offset:offset + 2]
            arguments[arguments.index("--write-out") + 1] = "\nCOVER_HTTP_METADATA\n%{http_code}\n%{content_type}"
        if insecure_skip_tls_verify:
            arguments.append("-k")
        elif ca_file:
            arguments.extend(["--cacert", str(pathutil.canonical(ca_file))])
        if token is not None:
            # Feed a curl config through stdin so the key never appears in
            # argv or a temporary file.  Config syntax preserves the space
            # after the header colon; some curl/HTTP-server combinations
            # normalize ``--header @-`` to ``Authorization:Bearer``.
            arguments.extend(["--config", "-"])
            standard_input = f'header = "Authorization: Bearer {token}"\n'
        if data is not None:
            request_path = temporary / "request.json"
            request_path.write_bytes(data)
            arguments.extend(["--data-binary", f"@{request_path}"])

        completed = subprocess.run(
            arguments,
            input=standard_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            message = " ".join(completed.stderr.strip().split())
            raise errors.ExecutorError(
                "COVER_API_TRANSPORT_ERROR",
                f"curl exited with {completed.returncode}",
                [message] if message and not memory_response else [],
            )
        response_text = ""
        metadata = completed.stdout
        if memory_response:
            if "\nCOVER_HTTP_METADATA\n" not in metadata:
                raise errors.ExecutorError("COVER_API_TRANSPORT_ERROR", "curl did not return HTTP metadata")
            response_text, metadata = metadata.rsplit("\nCOVER_HTTP_METADATA\n", 1)
        lines = metadata.splitlines()
        if not lines or not lines[0].isdigit():
            raise errors.ExecutorError("COVER_API_TRANSPORT_ERROR", "curl did not return an HTTP status")
        status = int(lines[0])
        content_type = lines[1] if len(lines) > 1 else ""
        body = response_text.encode("utf-8") if memory_response else response_path.read_bytes() if response_path.is_file() else b""
        return status, body, content_type


def verify_server_contract(
    base_url: str,
    contract: dict[str, Any],
    *,
    insecure_skip_tls_verify: bool,
    ca_file: str | None = None,
) -> None:
    url = base_url.rstrip("/") + contract["discoveryPath"]
    try:
        status, body, _ = _curl_request(
            url,
            method="GET",
            timeout=15,
            insecure_skip_tls_verify=insecure_skip_tls_verify,
            ca_file=ca_file,
        )
        if status != 200:
            raise errors.ExecutorError("API_CONTRACT_DISCOVERY_FAILED", f"Open API discovery returned HTTP {status}")
        document = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise errors.ExecutorError("API_CONTRACT_DISCOVERY_FAILED", f"Cannot read {url}: {exc}") from exc
    if document.get("info", {}).get("version") != contract["serverVersion"]:
        raise errors.ExecutorError("UNSUPPORTED_SERVER_VERSION", f"Expected {contract['serverVersion']}, got {document.get('info', {}).get('version')}")
    for name, operation in contract["operations"].items():
        server_op = document.get("paths", {}).get(operation["path"], {}).get(operation["method"].lower())
        if not server_op or server_op.get("operationId") != operation["operationId"]:
            raise errors.ExecutorError("API_CONTRACT_MISMATCH", f"Server does not match operation {name}")


def api_call(args: argparse.Namespace) -> dict[str, Any]:
    sensitive_response = bool(getattr(args, "sensitive_response", False))
    if sensitive_response and args.output:
        raise errors.ExecutorError("SENSITIVE_API_OUTPUT_REJECTED", "Sensitive API responses must remain in memory")
    contract = load_contract(args.contract)
    operation = contract["operations"].get(args.operation)
    if not operation:
        raise errors.ExecutorError("UNSUPPORTED_PUBLIC_API_OPERATION", f"Operation is not in the Open API v1 contract: {args.operation}")
    if operation.get("mutation") and not args.allow_mutation:
        raise errors.ExecutorError("API_MUTATION_APPROVAL_REQUIRED", f"{args.operation} requires --allow-mutation after explicit approval")
    validate_insecure_tls_request(args)
    insecure = bool(getattr(args, "insecure_skip_tls_verify", False))
    if args.ca_file and insecure:
        raise errors.ExecutorError("TLS_CONFIGURATION_CONFLICT", "A CA file cannot be combined with disabled TLS verification")
    base_url = args.base_url.rstrip("/")
    if not args.skip_contract_check:
        verify_server_contract(
            base_url,
            contract,
            insecure_skip_tls_verify=insecure,
            ca_file=args.ca_file,
        )
    query = parse_key_values(args.query)
    required_query = operation.get("requiredQuery", [])
    missing_query = [name for name in required_query if name not in query]
    if missing_query:
        raise errors.ExecutorError("INVALID_QUERY_PARAMETER", "Missing query parameters: " + ", ".join(missing_query))
    payload = None
    data = None
    if operation.get("request"):
        if not args.payload:
            raise errors.ExecutorError("INVALID_API_PAYLOAD", "--payload is required")
        payload = fileio.load_json(args.payload)
        validate_payload(operation, payload)
        data = fileio.canonical_json(payload)
    token = os.environ.get(args.token_env)
    if not token:
        if os.environ.get("COVER_CREDENTIAL_SESSION") == "1":
            raise errors.ExecutorError("API_TOKEN_MISSING", "The public API key is unavailable in the active credential session")
        raise errors.ExecutorError("API_TOKEN_MISSING", f"Set token in environment variable {args.token_env}")
    path_values = parse_key_values(getattr(args, "path_param", []) or [])
    if set(path_values) != set(operation.get("requiredPath", [])):
        raise errors.ExecutorError("INVALID_PATH_PARAMETER", "Supply exactly the path parameters required by this operation")
    api_path = operation["path"]
    for name, value in path_values.items():
        if not re.fullmatch(r"[0-9a-fA-F]{24}", value):
            raise errors.ExecutorError("INVALID_PATH_PARAMETER", f"{name} must be a Mongo ObjectId")
        api_path = api_path.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
    url = base_url + api_path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    status, body, content_type = _curl_request(
        url,
        method=operation["method"],
        timeout=args.timeout,
        insecure_skip_tls_verify=insecure,
        ca_file=args.ca_file,
        token=token,
        data=data,
        **({"memory_response": True} if sensitive_response else {}),
    )
    if status not in operation["successStatus"]:
        response_text = "[response omitted]" if sensitive_response else body[:65536].decode("utf-8", errors="replace")
        raise errors.ExecutorError("COVER_API_HTTP_ERROR", f"{args.operation} returned HTTP {status}", [response_text])
    decoded_json: Any | None = None
    if body.strip() and "json" in content_type:
        try:
            decoded_json = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            # Some mutation endpoints return an unstructured success body
            # while retaining an application/json content type.  HTTP status is
            # the operation contract here, so preserve the raw response rather
            # than reclassifying an accepted mutation as a client failure.
            decoded_json = None
    if args.output:
        output = pathutil.canonical(args.output, False)
        output.parent.mkdir(parents=True, exist_ok=True)
        if decoded_json is not None:
            fileio.atomic_write_text(output, json.dumps(decoded_json, ensure_ascii=False, indent=2) + "\n")
        elif not body.strip():
            fileio.atomic_write_json(output, {})
        else:
            output.write_bytes(body)
    parsed = decoded_json if decoded_json is not None else {"bytes": len(body)}
    return envelopes.envelope("CALLING_COVER_API", "SUCCEEDED", {"operation": args.operation, "status": status, "response": parsed})


def response_list(value: Any, key: str | None = None) -> list[Any]:
    if isinstance(value, list):
        return value
    if key and isinstance(value, dict) and isinstance(value.get(key), list):
        return value[key]
    raise errors.ExecutorError("INVALID_API_RESPONSE", f"Expected an array{f' in {key}' if key else ''}")


def parse_server_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 100000000000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    raise errors.ExecutorError("INVALID_INTERNAL_API_RESPONSE", f"Unsupported creationDate value: {value!r}")
