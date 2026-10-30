"""Canonical decision-value contracts shared by waiting and resume paths."""

from __future__ import annotations

from typing import Any


STRING_ARRAY_SUBSET_CHOICES = {"targetFrameworks", "visualStudioProjects"}
STRING_ARRAY_CHOICES = {"managedTasks", "expectedArtifacts", "javaCoverageArguments"}
JSON_OBJECT_CHOICES = {"originalBuildPlan", "targetToolPaths", "toolChainInfoSelection"}
POSITIVE_INTEGER_CHOICES = {"javaMajorVersion"}
NONEMPTY_STRING_CHOICES = {
    "agentInstallPath",
    "cscPath",
    "expectedArtifact",  # Legacy Visual Studio waiting jobs.
    "gccBuildDirectory",
    "gccBuildTool",
    "gccWrapperPrefix",
    "glueCodeCompiler",
    "jacovTool",
    "javaHome",
    "managedConfiguration",
    "managedBuildTool",
    "msbuildPath",
    "originalToolPath",
    "stm32CubeIDEExecutable",
    "stm32CubeIDEToolDirectory",
    "vsDevCmdPath",
}


def specification(key: str, candidates: Any) -> dict[str, Any]:
    """Describe the value the resume path accepts for one decision key."""
    if key in STRING_ARRAY_SUBSET_CHOICES:
        return {
            "mode": "STRING_ARRAY_SUBSET",
            "candidates": list(candidates) if isinstance(candidates, list) else [],
            "minItems": 1,
        }
    if key in STRING_ARRAY_CHOICES:
        return {"mode": "STRING_ARRAY"}
    if key in JSON_OBJECT_CHOICES:
        return {"mode": "JSON_OBJECT"}
    if key in POSITIVE_INTEGER_CHOICES:
        return {"mode": "POSITIVE_INTEGER"}
    if key in NONEMPTY_STRING_CHOICES:
        return {"mode": "NONEMPTY_STRING"}
    if isinstance(candidates, list) and candidates:
        return {"mode": "ONE_OF", "candidates": list(candidates)}
    if isinstance(candidates, str):
        return {"mode": "NONEMPTY_STRING"}
    return {"mode": "EXACT_VALUE", "value": candidates}
