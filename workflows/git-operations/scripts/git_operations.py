from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Direct invocation from a checkout must resolve the shared hub package
# without relying on PYTHONPATH or an installed distribution.  An installed
# distribution remains the fallback when this repository layout is absent.
_HUB_SRC = Path(__file__).resolve().parents[3] / "src"
if _HUB_SRC.is_dir() and str(_HUB_SRC) not in sys.path:
    sys.path.insert(0, str(_HUB_SRC))

from agent_workflow_hub.git_operations import (
    GitGuardError,
    committed_blob_argv,
    committed_tree_argv,
    exact_push_argv,
    head_commit_argv,
    remote_ref_query_argv,
    validate_commit_sha,
    validate_repository_relative_path,
)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(
            "repository path must be an absolute path"
        )
    return path


def _positive_depth(value: str) -> int:
    try:
        depth = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--depth must be a positive integer"
        ) from exc
    if depth < 1:
        raise argparse.ArgumentTypeError("--depth must be a positive integer")
    return depth


def _full_sha(value: str) -> str:
    try:
        return validate_commit_sha(value)
    except GitGuardError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _repository_relative_path(value: str) -> str:
    try:
        return validate_repository_relative_path(value)
    except GitGuardError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        required=True,
        type=_absolute_path,
        metavar="ABS_REPO",
        help="absolute path to the git repository",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git_operations.py",
        description=(
            "Run general Git operations through argv-only subprocess calls."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="show working tree status")
    _add_repo_argument(status)

    diff = subparsers.add_parser("diff", help="show unstaged changes")
    _add_repo_argument(diff)

    log = subparsers.add_parser("log", help="show commit history")
    _add_repo_argument(log)

    head_sha = subparsers.add_parser(
        "head-sha",
        help="resolve HEAD to one full commit SHA read-only",
    )
    _add_repo_argument(head_sha)

    list_tree = subparsers.add_parser(
        "list-tree",
        help="list one committed tree read-only with NUL-delimited paths",
    )
    _add_repo_argument(list_tree)
    list_tree.add_argument("--sha", required=True, type=_full_sha)

    show_file = subparsers.add_parser(
        "show-file",
        help="read one blob from an exact commit without reading the worktree",
    )
    _add_repo_argument(show_file)
    show_file.add_argument("--sha", required=True, type=_full_sha)
    show_file.add_argument(
        "--path",
        required=True,
        type=_repository_relative_path,
        metavar="REPO_RELATIVE_PATH",
    )

    clone = subparsers.add_parser(
        "clone",
        help="clone a repository, optionally shallow",
    )
    clone.add_argument(
        "--depth",
        type=_positive_depth,
        help="create a shallow clone of the given depth",
    )
    clone.add_argument("url", help="repository URL or local path")
    clone.add_argument(
        "dest",
        type=_absolute_path,
        help="absolute destination directory",
    )

    add = subparsers.add_parser("add", help="stage paths")
    _add_repo_argument(add)
    add.add_argument("paths", nargs="+", help="paths to stage")

    commit = subparsers.add_parser("commit", help="create a commit")
    _add_repo_argument(commit)
    commit.add_argument(
        "-m",
        "--message",
        required=True,
        help="commit message",
    )

    branch_create = subparsers.add_parser(
        "branch-create",
        help="create a branch",
    )
    _add_repo_argument(branch_create)
    branch_create.add_argument("name", help="branch name")

    checkout = subparsers.add_parser("checkout", help="switch branches")
    _add_repo_argument(checkout)
    checkout.add_argument("branch", help="branch to check out")

    merge = subparsers.add_parser("merge", help="merge a branch")
    _add_repo_argument(merge)
    merge.add_argument("branch", help="branch to merge")

    push = subparsers.add_parser("push", help="push to a remote")
    _add_repo_argument(push)
    push.add_argument("remote", help="remote name")
    push.add_argument("branch", help="branch to push")

    push_exact = subparsers.add_parser(
        "push-exact",
        help="push one full commit to one canonical branch ref without force",
    )
    _add_repo_argument(push_exact)
    push_exact.add_argument("--url", required=True, help="HTTPS remote URL")
    push_exact.add_argument("--sha", required=True, help="full lowercase commit SHA")
    push_exact.add_argument("--branch", required=True, help="canonical branch ref")

    ls_remote_ref = subparsers.add_parser(
        "ls-remote-ref",
        help="query one canonical remote branch ref read-only",
    )
    ls_remote_ref.add_argument("--url", required=True, help="HTTPS remote URL")
    ls_remote_ref.add_argument("--branch", required=True, help="canonical branch ref")

    return parser


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "status":
        return _run(["git", "-C", str(args.repo), "status"])
    if args.command == "diff":
        return _run(["git", "-C", str(args.repo), "diff"])
    if args.command == "log":
        return _run(["git", "-C", str(args.repo), "log"])
    if args.command == "head-sha":
        command = head_commit_argv()
        return _run([command[0], "-C", str(args.repo), *command[1:]])
    if args.command == "list-tree":
        command = committed_tree_argv(args.sha)
        return _run([command[0], "-C", str(args.repo), *command[1:]])
    if args.command == "show-file":
        command = committed_blob_argv(args.sha, args.path)
        return _run([command[0], "-C", str(args.repo), *command[1:]])
    if args.command == "clone":
        command = ["git", "clone"]
        if args.depth is not None:
            command += ["--depth", str(args.depth)]
        command += [args.url, str(args.dest)]
        return _run(command)
    if args.command == "add":
        return _run(["git", "-C", str(args.repo), "add", "--", *args.paths])
    if args.command == "commit":
        return _run(
            ["git", "-C", str(args.repo), "commit", "-m", args.message]
        )
    if args.command == "branch-create":
        return _run(["git", "-C", str(args.repo), "branch", args.name])
    if args.command == "checkout":
        return _run(["git", "-C", str(args.repo), "checkout", args.branch])
    if args.command == "merge":
        return _run(
            ["git", "-C", str(args.repo), "merge", "--no-edit", args.branch]
        )
    if args.command == "push":
        return _run(
            ["git", "-C", str(args.repo), "push", args.remote, args.branch]
        )
    if args.command == "push-exact":
        argv = exact_push_argv(args.url, args.sha, args.branch)
        return _run([argv[0], "-C", str(args.repo), *argv[1:]])
    if args.command == "ls-remote-ref":
        return _run(list(remote_ref_query_argv(args.url, args.branch)))
    return 2


if __name__ == "__main__":
    sys.exit(main())
