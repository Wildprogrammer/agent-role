from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import CapabilityContract

if TYPE_CHECKING:
    from .repository import ValidationIssue


@dataclass(frozen=True)
class ConfigTemplateDescriptor:
    label: str
    relative_path: str
    output_name: str
    scope: str
    required: bool
    content: str = field(repr=False)
    sha256: str


@dataclass(frozen=True)
class WorkflowDescriptor:
    name: str
    description: str
    required_capabilities: tuple[str, ...]
    capability_slots: Mapping[str, tuple[str, ...]]
    supported_hosts: tuple[str, ...]
    config_templates: tuple[ConfigTemplateDescriptor, ...]
    entrypoints: Mapping[str, str]


@dataclass(frozen=True)
class RepositoryCatalog:
    root: Path
    workflows: tuple[WorkflowDescriptor, ...]
    capabilities: Mapping[str, CapabilityContract]


class RepositoryCatalogError(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...] | list[ValidationIssue]):
        self.issues = tuple(issues)
        message = "; ".join(
            f"{issue.code}: {issue.path}: {issue.message}"
            for issue in self.issues
        )
        super().__init__(message or "Repository catalog could not be loaded")


def load_repository_catalog(root: Path) -> RepositoryCatalog:
    from .repository import (
        ValidationIssue,
        _GuardedRepository,
        _SafeFilesystemError,
        _load_guarded_repository,
    )

    canonical_root = Path(os.path.abspath(root))
    try:
        with _GuardedRepository(canonical_root) as guarded:
            loaded = _load_guarded_repository(
                canonical_root,
                guarded,
                require_project_markers=True,
            )
    except _SafeFilesystemError:
        raise RepositoryCatalogError(
            (
                ValidationIssue(
                    "invalid-root",
                    canonical_root,
                    "Repository root could not be guarded safely",
                ),
            )
        ) from None
    if loaded.issues:
        raise RepositoryCatalogError(loaded.issues)
    return loaded.catalog
