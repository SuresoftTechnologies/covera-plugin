"""Require current failed-build evidence before adding workaround options."""

from dataclasses import dataclass
from pathlib import Path

from cover_build.common import fileio, pathutil
from cover_build.core.errors import ExecutorError, WorkflowWaiting


@dataclass(frozen=True)
class VerifiedOptionReuse:
    """Process-local result of the workflow's successful profile validation."""

    job_id: str
    source_job_id: str
    arguments: tuple[str, ...]
    legacy_java_arguments: tuple[str, ...] = ()


def verified_profile_options(profile, job_id):
    """Call only after environment_profile.load returns a compatible HIT."""
    if profile is None:
        return None
    proof = profile["choices"].get("coverageOptionRecovery")
    arguments = proof.get("arguments") if isinstance(proof, dict) else None
    if "coverageOptionRecovery" not in profile["choices"]:
        # Older successful profiles predate failure evidence. Trust only the
        # exact saved settings, after the ordinary profile checks have passed.
        plan = profile["choices"].get("originalBuildPlan") or {}
        injection = plan.get("coverageInjection") if isinstance(plan, dict) else None
        arguments = injection.get("configureArguments") if isinstance(injection, dict) else None
        java_arguments = profile["choices"].get("javaCoverageArguments")
        if isinstance(java_arguments, list) and java_arguments and all(isinstance(v, str) for v in java_arguments):
            return VerifiedOptionReuse(job_id, profile["sourceJobId"], (), tuple(java_arguments))
    if not arguments or not isinstance(arguments, list) or not all(isinstance(v, str) for v in arguments):
        return None
    return VerifiedOptionReuse(job_id, profile["sourceJobId"], tuple(arguments))


def additional_arguments(original, proposed):
    """Return additions without allowing removal or reordering of original argv."""
    index = 0
    additions = []
    for value in proposed:
        if index < len(original) and value == original[index]:
            index += 1
        else:
            additions.append(value)
    if index != len(original):
        raise ExecutorError(
            "ORIGINAL_BUILD_ARGUMENTS_CHANGED",
            "Coverage options may not remove or reorder the customer's build arguments",
        )
    return additions


def require_failure(job, recovery, arguments, *, phases, decision_choices, verified_reuse=None, coverage_arguments=None):
    """Check execution/quote facts; the model explains the causal connection."""
    if not arguments:
        return None
    if (isinstance(verified_reuse, VerifiedOptionReuse)
            and verified_reuse.job_id == job.get("jobId")
            and (verified_reuse.arguments == tuple(arguments)
                 or (verified_reuse.legacy_java_arguments
                     and coverage_arguments is not None
                     and verified_reuse.legacy_java_arguments == tuple(coverage_arguments)))):
        return {"source": "VERIFIED_ENVIRONMENT_PROFILE", "sourceJobId": verified_reuse.source_job_id,
                "jobId": job["jobId"], "arguments": list(arguments)}
    evidence = []
    try:
        if not isinstance(job, dict) or not isinstance(recovery, dict):
            raise ValueError("No current-job failure diagnosis was supplied")
        if recovery.get("jobId") != job["jobId"] or recovery.get("arguments") != list(arguments):
            raise ValueError("Diagnosis must identify this job and these exact additional arguments")
        reason = recovery.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Explain how the observed failure requires these arguments")
        log = Path(recovery["log"])
        if not log.is_absolute() or not pathutil.path_within(log, job["directories"]["logs"]):
            raise ValueError("Use an attempt log under the current job's logs directory")
        metrics = fileio.load_json(log.with_suffix(".metrics.json"))
        if not isinstance(metrics, dict):
            raise ValueError("Command metrics must be an object")
        exit_code = metrics.get("exitCode")
        if (type(exit_code) is not int or exit_code == 0 or metrics.get("logComplete") is not True
                or metrics.get("timedOut") is True or metrics.get("phase") not in phases
                or not pathutil.same_path(metrics.get("log", ""), log)):
            raise ValueError("The referenced command must have actually failed in the relevant build phase")
        if recovery.get("sha256") != pathutil.sha256_file(log):
            raise ValueError("The failure log changed after diagnosis")
        line = recovery.get("line")
        quote = recovery.get("text")
        if type(line) is not int or line < 1 or not isinstance(quote, str) or not quote.strip():
            raise ValueError("Cite a nonempty failure-log line")
        with log.open(encoding="utf-8", errors="replace") as stream:
            actual = next((text.rstrip("\r\n") for number, text in enumerate(stream, 1) if number == line), None)
        if actual != quote:
            raise ValueError("The cited failure line does not match the current log")
        return dict(recovery)
    except (OSError, ValueError, KeyError, TypeError, ExecutorError) as exc:
        evidence.append(str(exc))
    raise WorkflowWaiting(
        "BUILD_OPTION_RECOVERY_EVIDENCE_REQUIRED",
        "Keep the original options on the first attempt. Add workaround options only after diagnosing an actual current-job failure",
        {**decision_choices, "coverageOptionRecovery": {}},
        decision_owner="MODEL", purpose="BUILD_ENVIRONMENT", evidence=evidence,
    )
