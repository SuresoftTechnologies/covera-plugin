"""Dependencies shared by environment pipelines.

Pipelines receive these bindings from the public runner. This keeps their
end-to-end flow readable without importing the runner or duplicating its job,
state, authentication, and evidence helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PipelineRuntime:
    argument: Callable[..., Any]
    fallback_source: Callable[..., Any]
    finalize_environment_survey_plan: Callable[..., Any]
    job_project_input_fingerprint: Callable[..., Any]
    job_work_path: Callable[..., Any]
    probe_windows_gcc_ar_wrapper: Callable[..., Any]
    record: Callable[..., Any]
    record_coverage_injection_evidence: Callable[..., Any]
    resolve_fallback_artifact: Callable[..., Any]
    selected_cover_config: Callable[..., Any]
    write_json: Callable[..., Any]
    write_original_build_evidence: Callable[..., Any]
