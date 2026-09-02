from pathlib import Path
import os
import pytest
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "git_operations.py"


def invoke(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        check=False,
    )


def run_git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], capture_output=True, check=False)


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    initialized = run_git("init", "-b", "master", str(path))
    assert initialized.returncode == 0, initialized.stderr
    for name, value in (
        ("user.name", "Test User"),
        ("user.email", "test@example.invalid"),
    ):
        configured = run_git("-C", str(path), "config", name, value)
        assert configured.returncode == 0, configured.stderr
    (path / "initial.txt").write_text("initial\n", encoding="utf-8")
    run_git("-C", str(path), "add", "initial.txt")
    committed = run_git("-C", str(path), "commit", "-m", "initial commit")
    assert committed.returncode == 0, committed.stderr
    return path


def bare_remote(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    initialized = run_git("init", "--bare", str(path))
    assert initialized.returncode == 0, initialized.stderr
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    added = run_git("-C", str(repo), "add", "--", name)
    assert added.returncode == 0, added.stderr
    committed = run_git("-C", str(repo), "commit", "-m", message)
    assert committed.returncode == 0, committed.stderr


def test_status_reports_clean_worktree(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")

    result = invoke("status", "--repo", str(repo))

    assert result.returncode == 0, result.stderr
    assert b"nothing to commit" in result.stdout


def test_diff_passes_stdout_through_when_changes_exist(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")

    clean = invoke("diff", "--repo", str(repo))
    assert clean.returncode == 0
    assert clean.stdout == b""

    (repo / "initial.txt").write_text("changed\n", encoding="utf-8")
    changed = invoke("diff", "--repo", str(repo))

    assert changed.returncode == 0
    assert b"+changed" in changed.stdout


def test_log_lists_commits_and_passes_through_empty_repository_error(
    tmp_path: Path,
):
    repo = init_repo(tmp_path / "repo")
    commit_file(repo, "second.txt", "two\n", "second commit")

    result = invoke("log", "--repo", str(repo))

    assert result.returncode == 0, result.stderr
    assert b"second commit" in result.stdout
    assert b"initial commit" in result.stdout

    empty = tmp_path / "empty"
    empty.mkdir()
    initialized = run_git("init", "-b", "master", str(empty))
    assert initialized.returncode == 0, initialized.stderr
    failed = invoke("log", "--repo", str(empty))

    assert failed.returncode != 0
    assert b"fatal:" in failed.stderr


def test_clone_depth_creates_shallow_clone_from_local_bare_remote(
    tmp_path: Path,
):
    source = init_repo(tmp_path / "source")
    commit_file(source, "one.txt", "1\n", "commit one")
    commit_file(source, "two.txt", "2\n", "commit two")
    remote = bare_remote(tmp_path / "remote.git")
    run_git("-C", str(source), "remote", "add", "origin", str(remote))
    pushed = run_git("-C", str(source), "push", "origin", "master")
    assert pushed.returncode == 0, pushed.stderr

    dest = tmp_path / "shallow"
    result = invoke("clone", "--depth", "1", remote.as_uri(), str(dest))

    assert result.returncode == 0, result.stderr
    count = run_git("-C", str(dest), "rev-list", "--count", "HEAD")
    assert count.returncode == 0, count.stderr
    assert count.stdout.strip() == b"1"
    assert (dest / ".git" / "shallow").is_file()
    assert (dest / "two.txt").read_text(encoding="utf-8") == "2\n"
    assert (dest / "initial.txt").is_file()


def test_add_commit_push_updates_local_bare_remote(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    remote = bare_remote(tmp_path / "remote.git")
    run_git("-C", str(repo), "remote", "add", "origin", str(remote))
    seeded = run_git("-C", str(repo), "push", "-u", "origin", "master")
    assert seeded.returncode == 0, seeded.stderr

    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    added = invoke("add", "--repo", str(repo), str(repo / "feature.txt"))
    assert added.returncode == 0, added.stderr
    committed = invoke("commit", "--repo", str(repo), "-m", "feature commit")
    assert committed.returncode == 0, committed.stderr
    pushed = invoke("push", "--repo", str(repo), "origin", "master")
    assert pushed.returncode == 0, pushed.stderr

    local_head = run_git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    remote_head = run_git("--git-dir", str(remote), "rev-parse", "master").stdout.strip()
    assert local_head == remote_head


def test_add_accepts_paths_with_spaces_without_shell(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    target = repo / "new file.txt"
    target.write_text("spaced\n", encoding="utf-8")

    result = invoke("add", "--repo", str(repo), str(target))

    assert result.returncode == 0, result.stderr
    staged = run_git("-C", str(repo), "diff", "--cached", "--name-only")
    assert b"new file.txt" in staged.stdout


def test_branch_create_checkout_merge(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")

    created = invoke("branch-create", "--repo", str(repo), "feature")
    assert created.returncode == 0, created.stderr
    branches = run_git("-C", str(repo), "branch", "--list")
    assert b"feature" in branches.stdout

    checked = invoke("checkout", "--repo", str(repo), "feature")
    assert checked.returncode == 0, checked.stderr
    commit_file(repo, "feature.txt", "feature\n", "feature commit")

    invoke("checkout", "--repo", str(repo), "master")
    commit_file(repo, "master.txt", "master\n", "master commit")

    merged = invoke("merge", "--repo", str(repo), "feature")

    assert merged.returncode == 0, merged.stderr
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert (repo / "master.txt").is_file()
    ancestor = run_git(
        "-C",
        str(repo),
        "merge-base",
        "--is-ancestor",
        "feature",
        "master",
    )
    assert ancestor.returncode == 0
    assert b"Merge" in run_git(
        "-C",
        str(repo),
        "log",
        "--oneline",
        "-1",
    ).stdout


def test_merge_argv_contains_no_edit(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    captured: list[list[str]] = []

    import importlib.util

    spec = importlib.util.spec_from_file_location("git_operations_argv_spy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_run(
        argv: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main(["merge", "--repo", str(repo), "feature"]) == 0
    assert captured, "merge must invoke git exactly once"
    assert "--no-edit" in captured[0]
    assert captured[0] == [
        "git",
        "-C",
        str(repo),
        "merge",
        "--no-edit",
        "feature",
    ]


def test_push_exact_builds_exact_non_force_argv(tmp_path: Path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "git_operations_push_exact_argv_spy",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: list[list[str]] = []

    def fake_run(
        argv: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    sha = "a" * 40
    repo = tmp_path / "repo"
    repo.mkdir()

    exit_code = module.main(
        [
            "push-exact",
            "--repo",
            str(repo),
            "--url",
            "https://git.example.test/org/repo.git",
            "--sha",
            sha,
            "--branch",
            "test/test-20260821-001",
        ]
    )

    assert exit_code == 0
    assert captured == [
        [
            "git",
            "-C",
            str(repo),
            "push",
            "--porcelain",
            "https://git.example.test/org/repo.git",
            f"{sha}:refs/heads/test/test-20260821-001",
        ]
    ]
    assert all("--force" not in argument for argument in captured[0])
    assert isinstance(captured[0], list)


def test_push_exact_requires_explicit_absolute_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "git_operations_push_exact_repo_requirement",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_run(
        argv: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    sha = "a" * 40
    base = [
        "--url",
        "https://git.example.test/org/repo.git",
        "--sha",
        sha,
        "--branch",
        "test/test-20260821-001",
    ]

    with pytest.raises(SystemExit):
        module.main(["push-exact", *base])
    captured = capsys.readouterr()
    assert "--repo" in captured.err

    with pytest.raises(SystemExit):
        module.main(["push-exact", "--repo", "relative-repo", *base])
    captured = capsys.readouterr()
    assert "absolute" in captured.err


def test_ls_remote_ref_builds_exact_read_only_argv(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "git_operations_ls_remote_ref_argv_spy",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: list[list[str]] = []

    def fake_run(
        argv: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    exit_code = module.main(
        [
            "ls-remote-ref",
            "--url",
            "https://git.example.test/org/repo.git",
            "--branch",
            "test/test-20260821-001",
        ]
    )

    assert exit_code == 0
    assert captured == [
        [
            "git",
            "ls-remote",
            "--heads",
            "https://git.example.test/org/repo.git",
            "refs/heads/test/test-20260821-001",
        ]
    ]


def test_merge_succeeds_when_editor_is_configured_to_fail(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    created = invoke("branch-create", "--repo", str(repo), "feature")
    assert created.returncode == 0, created.stderr
    checked = invoke("checkout", "--repo", str(repo), "feature")
    assert checked.returncode == 0, checked.stderr
    commit_file(repo, "feature.txt", "feature\n", "feature commit")

    invoke("checkout", "--repo", str(repo), "master")
    commit_file(repo, "master.txt", "master\n", "master commit")

    env = dict(os.environ)
    env["GIT_MERGE_AUTOEDIT"] = "yes"
    env["GIT_EDITOR"] = "C:\\definitely-missing-git-editor.exe"
    merged = subprocess.run(
        [sys.executable, str(SCRIPT), "merge", "--repo", str(repo), "feature"],
        capture_output=True,
        check=False,
        env=env,
    )

    assert merged.returncode == 0, merged.stderr
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert (repo / "master.txt").is_file()


def test_checkout_does_not_discard_user_modifications(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    created = invoke("branch-create", "--repo", str(repo), "feature")
    assert created.returncode == 0, created.stderr
    checked = invoke("checkout", "--repo", str(repo), "feature")
    assert checked.returncode == 0, checked.stderr
    (repo / "initial.txt").write_text("feature version\n", encoding="utf-8")
    run_git("-C", str(repo), "add", "initial.txt")
    committed = run_git("-C", str(repo), "commit", "-m", "feature file version")
    assert committed.returncode == 0, committed.stderr
    invoke("checkout", "--repo", str(repo), "master")

    (repo / "initial.txt").write_text("user change\n", encoding="utf-8")
    result = invoke("checkout", "--repo", str(repo), "feature")

    assert result.returncode != 0
    assert b"local changes" in result.stderr
    assert (repo / "initial.txt").read_text(encoding="utf-8") == "user change\n"


def test_repo_path_must_be_explicit_absolute_path(tmp_path: Path):
    init_repo(tmp_path / "repo")

    result = invoke("status", "--repo", "relative-repo")

    assert result.returncode != 0
    assert b"absolute" in result.stderr


def test_cli_never_offers_force_and_rejects_it(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")

    help_result = invoke("--help")
    assert help_result.returncode == 0
    assert b"--force" not in help_result.stdout

    rejected = invoke("push", "--repo", str(repo), "--force", "origin", "master")
    assert rejected.returncode != 0
    assert b"usage:" in rejected.stderr


def test_direct_invocation_works_without_pythonpath_or_installed_package(
    tmp_path: Path,
):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    workdir = tmp_path / "non-repository"
    workdir.mkdir()

    result = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--help"],
        capture_output=True,
        check=False,
        env=env,
        cwd=str(workdir),
    )

    assert result.returncode == 0, result.stderr
    assert b"usage:" in result.stdout
    assert b"push-exact" in result.stdout


def test_head_sha_returns_full_commit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = invoke("head-sha", "--repo", str(repo))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == run_git(
        "-C", str(repo), "rev-parse", "HEAD"
    ).stdout.strip()


def test_list_tree_preserves_blob_metadata_and_nul_paths(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    commit_file(repo, "name with spaces.txt", "committed\n", "add spaced file")
    sha = run_git("-C", str(repo), "rev-parse", "HEAD").stdout.strip().decode()

    result = invoke("list-tree", "--repo", str(repo), "--sha", sha)

    assert result.returncode == 0, result.stderr
    assert b"100644 blob " in result.stdout
    assert b"\tname with spaces.txt\x00" in result.stdout


def test_committed_snapshot_ignores_working_tree_changes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    nested = repo / "docs"
    nested.mkdir()
    commit_file(repo, "docs/guide.md", "committed", "add guide")
    sha = run_git("-C", str(repo), "rev-parse", "HEAD").stdout.strip().decode()
    (nested / "guide.md").write_text("dirty", encoding="utf-8")

    result = invoke(
        "show-file",
        "--repo",
        str(repo),
        "--sha",
        sha,
        "--path",
        "docs/guide.md",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == b"committed"
