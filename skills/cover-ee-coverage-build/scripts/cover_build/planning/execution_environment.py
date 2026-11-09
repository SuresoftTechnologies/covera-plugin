"""Model review of execution compatibility, shared by every build pipeline."""
from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

from cover_build.common import fileio, pathutil
from cover_build.core.errors import WorkflowWaiting


def host_context() -> dict[str, Any]:
    # Do not persist the process environment: it can contain credentials.
    return {
        "os": platform.system(),
        "architecture": platform.machine(),
        "availableLaunchers": {
            name: shutil.which(name)
            for name in ("cmd", "powershell", "pwsh", "sh", "bash", "wsl", "docker")
        },
    }


def review_context(job: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    paths = set(plan.get("evidence") or [])
    for key in ("adapterEntry", "buildTool"):
        if plan.get(key):
            paths.add(plan[key])
    temporary = Path(job["directories"]["temporary"])
    # Refresh file hashes instead of trusting an earlier discovery snapshot.
    for name in ("build-evidence.json", "selected-build-references.json"):
        source = temporary / name
        if source.is_file():
            data = fileio.load_json(source)
            for item in data.get("files", []):
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths.add(item["path"])
    files = []
    for value in sorted(paths):
        path = Path(value)
        files.append({"path": str(path), "exists": path.is_file(),
                      "sha256": pathutil.sha256_file(path) if path.is_file() else None})
    return fileio.add_integrity({
        "schema": "cover-execution-environment-context/v1",
        "jobId": job["jobId"], "host": host_context(),
        "plan": plan, "files": files,
    })


def require_review(job: dict[str, Any], plan: dict[str, Any]) -> None:
    temporary = Path(job["directories"]["temporary"])
    context = review_context(job, plan)
    context_path = temporary / "execution-environment-context.json"
    fileio.write_json(context_path, context)
    history_path = temporary / "decision-history.json"
    review = None
    if history_path.is_file():
        history = fileio.load_json(history_path)
        fileio.verify_integrity(history)
        if history.get("jobId") == job["jobId"]:
            review = history.get("choices", {}).get("executionEnvironmentReview")
    issues = []
    if not isinstance(review, dict):
        issues.append("A model execution-environment review is required")
    else:
        if review.get("jobId") != job["jobId"] or review.get("contextDigest") != context["integrity"]:
            issues.append("Review does not match the current job, plan, host or evidence files")
        if review.get("compatible") is not True:
            issues.append("The selected plan has not been judged compatible")
        for field in ("executionOs", "shellAssessment", "toolchainAssessment",
                      "prerequisitesAssessment", "candidateAssessment", "scopeAssessment"):
            if not isinstance(review.get(field), str) or not review[field].strip():
                issues.append(f"Missing {field}")
        mode = review.get("executionMode")
        if mode not in {"HOST", "WSL", "CONTAINER", "REMOTE"}:
            issues.append("Invalid executionMode")
        elif mode == "HOST" and review.get("executionOs") != context["host"]["os"]:
            issues.append("Host execution OS contradicts the detected host")
        elif mode != "HOST" and not str(review.get("launcherAssessment") or "").strip():
            issues.append("A separate environment requires launcher and readiness evidence")
        references = review.get("evidencePaths")
        known = {item["path"] for item in context["files"] if item["exists"]}
        if (not isinstance(references, list) or not references
                or any(not isinstance(p, str) or p not in known for p in references)):
            issues.append("Review must cite existing files in the current context")
    if issues:
        raise WorkflowWaiting(
            "EXECUTION_ENVIRONMENT_REVIEW_REQUIRED",
            "; ".join(issues),
            {"executionEnvironmentReview": {"contextPath": str(context_path),
                                             "contextDigest": context["integrity"]}},
            decision_owner="MODEL", purpose="BUILD_ENVIRONMENT", evidence=[str(context_path)],
        )
    fileio.write_json(temporary / "execution-environment-review.json", fileio.add_integrity({
        "jobId": job["jobId"], "contextDigest": context["integrity"], "review": review,
    }))
