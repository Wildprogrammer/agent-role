"""Exact non-force Git argv construction shared across capabilities."""

from __future__ import annotations

from .validation import (
    normalize_https_push_url,
    validate_branch_ref,
    validate_commit_sha,
    validate_repository_relative_path,
)


def head_commit_argv() -> tuple[str, ...]:
    return ("git", "rev-parse", "--verify", "HEAD^{commit}")


def committed_tree_argv(commit_sha: str) -> tuple[str, ...]:
    return (
        "git",
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        validate_commit_sha(commit_sha),
    )


def committed_blob_argv(commit_sha: str, relative_path: str) -> tuple[str, ...]:
    sha = validate_commit_sha(commit_sha)
    path = validate_repository_relative_path(relative_path)
    return ("git", "cat-file", "blob", f"{sha}:{path}")


def exact_refspec(commit_sha: str, branch: str) -> str:
    return f"{validate_commit_sha(commit_sha)}:refs/heads/{validate_branch_ref(branch)}"


def exact_push_argv(push_url: str, commit_sha: str, branch: str) -> tuple[str, ...]:
    return (
        "git",
        "push",
        "--porcelain",
        normalize_https_push_url(push_url),
        exact_refspec(commit_sha, branch),
    )


def remote_ref_query_argv(push_url: str, branch: str) -> tuple[str, ...]:
    return (
        "git",
        "ls-remote",
        "--heads",
        normalize_https_push_url(push_url),
        f"refs/heads/{validate_branch_ref(branch)}",
    )
