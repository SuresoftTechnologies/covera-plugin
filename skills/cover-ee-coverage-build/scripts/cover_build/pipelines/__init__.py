"""Readable end-to-end workflows for each supported build environment."""

from cover_build.pipelines.registry import SUPPORTED_PIPELINES
from cover_build.pipelines.registry import load_pipeline

__all__ = ["SUPPORTED_PIPELINES", "load_pipeline"]
