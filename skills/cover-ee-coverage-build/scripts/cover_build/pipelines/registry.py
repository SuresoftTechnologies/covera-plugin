"""One explicit lookup table for every supported coverage pipeline."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


SUPPORTED_PIPELINES = {
    "VISUAL_STUDIO": "cover_build.pipelines.visual_studio",
    "CMAKE": "cover_build.pipelines.gcc",
    "MAKE": "cover_build.pipelines.gcc",
    "STM32CUBEIDE": "cover_build.pipelines.stm32cubeide",
    "CSHARP": "cover_build.pipelines.csharp",
    "JAVA": "cover_build.pipelines.java",
}


def load_pipeline(build_system: str) -> ModuleType:
    try:
        module_name = SUPPORTED_PIPELINES[build_system]
    except KeyError as exc:
        raise ValueError(f"Unsupported coverage pipeline: {build_system}") from exc
    return import_module(module_name)
