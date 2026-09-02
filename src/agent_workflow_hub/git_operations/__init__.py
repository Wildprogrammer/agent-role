"""Shared guarded Git operations for lifecycle and general workflows."""

from .argv import (
    committed_blob_argv,
    committed_tree_argv,
    exact_push_argv,
    exact_refspec,
    head_commit_argv,
    remote_ref_query_argv,
)
from .validation import (
    GitGuardError,
    WorktreeEvidence,
    normalize_https_push_url,
    validate_branch_ref,
    validate_clean_worktree,
    validate_commit_sha,
    validate_repository_relative_path,
)

__all__ = (
    "GitGuardError",
    "WorktreeEvidence",
    "committed_blob_argv",
    "committed_tree_argv",
    "exact_push_argv",
    "exact_refspec",
    "head_commit_argv",
    "normalize_https_push_url",
    "remote_ref_query_argv",
    "validate_branch_ref",
    "validate_clean_worktree",
    "validate_commit_sha",
    "validate_repository_relative_path",
)
