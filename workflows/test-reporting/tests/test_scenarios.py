import ast
import os
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / "workflows" / "test-reporting"
SKILL_PATH = WORKFLOW / "SKILL.md"
ROLE_PATH = WORKFLOW / "roles" / "test-results-analyst.md"
TEMPLATE_PATH = WORKFLOW / "templates" / "test-report.md"
OPTIONAL_CAPABILITY_HEADING = "## Jenkins/JUnit 可选输入能力"
_NON_REPO_DIR_PARTS = frozenset(
    {
        ".codex-remote-attachments",
        ".env",
        ".git",
        ".mypy_cache",
        "node_modules",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        ".venv",
        "__pycache__",
        ".worktrees",
        "venv",
    }
)
_REPORT_AUTHORITY_DEFINITIONS = {
    "ExecutionSummary": "src/agent_workflow_hub/test_reporting/model.py",
    "TestReportModel": "src/agent_workflow_hub/test_reporting/model.py",
    "ReportContext": "src/agent_workflow_hub/test_reporting/model.py",
    "JunitEvidence": "src/agent_workflow_hub/test_reporting/model.py",
    "JenkinsClassification": "src/agent_workflow_hub/test_reporting/model.py",
    "JenkinsAttempt": "src/agent_workflow_hub/test_reporting/model.py",
    "classify_jenkins_attempt": (
        "src/agent_workflow_hub/test_reporting/classify.py"
    ),
    "render_test_report": "src/agent_workflow_hub/test_reporting/render.py",
    "report_sha256": "src/agent_workflow_hub/test_reporting/files.py",
}
_LEGACY_DEF_SUBSTRING = "def render_test_report"
_LEGACY_SCOPE_PREFIX = "src/agent_workflow_hub"


def _load_workflow() -> tuple[dict[str, str], str]:
    frontmatter, body = parse_markdown(SKILL_PATH)
    validate_skill(SKILL_PATH, frontmatter, body)
    return frontmatter, body


def _optional_capability_section(body: str) -> str:
    assert OPTIONAL_CAPABILITY_HEADING in body
    return body.split(OPTIONAL_CAPABILITY_HEADING, 1)[1]


def _python_files(root: Path) -> list[Path]:
    """Recursively enumerate ordinary ``*.py`` files physically under ``root``.

    ``os.walk`` runs with ``followlinks=False`` and every directory and file is
    filtered through ``_is_link_or_reparse`` before it is entered or returned,
    so symlinks, Windows junctions, and other reparse points pointing outside
    ``root`` are never traversed and their contents are never read. Excluded
    directory names are matched case-insensitively (``VENV``, ``.ENV``,
    ``NODE_MODULES``, ``SITE-PACKAGES``, caches, and attachment names).
    """

    excluded = {part.lower() for part in _NON_REPO_DIR_PARTS}
    result: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        kept: list[str] = []
        for name in dirnames:
            if name.lower() in excluded:
                continue
            candidate = Path(directory) / name
            if _is_link_or_reparse(candidate):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if not name.lower().endswith(".py"):
                continue
            candidate = Path(directory) / name
            if _is_link_or_reparse(candidate):
                continue
            result.append(candidate)
    return sorted(result)


def _is_link_or_reparse(path: Path) -> bool:
    """Detect symlinks, Windows junctions, and reparse points without resolving."""

    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt":
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(st, "st_file_attributes", 0) & reparse)
    return False


def test_python_files_excludes_directory_names_case_insensitively() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        for name in (
            "VENV",
            ".VENV",
            ".ENV",
            "NODE_MODULES",
            "SITE-PACKAGES",
            "__PYCACHE__",
            ".GIT",
            ".PYTEST_CACHE",
            ".WORKTREES",
            ".CODEX-REMOTE-ATTACHMENTS",
        ):
            directory = root / name
            directory.mkdir()
            (directory / "evil.py").write_text(
                "def render_test_report(report):\n    return 'bad'\n",
                encoding="utf-8",
            )

        files = [path.relative_to(root).as_posix() for path in _python_files(root)]

        assert files == ["main.py"]


def test_python_files_does_not_follow_directory_or_file_symlinks() -> None:
    with TemporaryDirectory() as raw:
        outside = Path(raw) / "outside"
        root = Path(raw) / "repo"
        outside.mkdir()
        root.mkdir()
        (outside / "outside.py").write_text("OUTSIDE = True\n", encoding="utf-8")
        (root / "real.py").write_text("REAL = True\n", encoding="utf-8")
        try:
            os.symlink(
                outside,
                root / "linked_dir",
                target_is_directory=True,
            )
            os.symlink(outside / "outside.py", root / "linked_file.py")
        except OSError:
            pytest.skip("symlink creation is not permitted on this platform")

        files = sorted(
            path.relative_to(root).as_posix()
            for path in _python_files(root)
        )

        assert files == ["real.py"]


def test_python_files_skips_reparse_point_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        trap = root / "trap"
        trap.mkdir()
        (trap / "evil.py").write_text("EVIL = True\n", encoding="utf-8")
        (root / "real.py").write_text("REAL = True\n", encoding="utf-8")
        original = _is_link_or_reparse

        def fake(path: Path) -> bool:
            return path == trap or original(path)

        monkeypatch.setattr(
            sys.modules[__name__],
            "_is_link_or_reparse",
            fake,
        )

        files = sorted(
            path.relative_to(root).as_posix()
            for path in _python_files(root)
        )

        assert files == ["real.py"]


def test_python_files_skips_real_windows_junction() -> None:
    if os.name != "nt":
        pytest.skip("Windows junction creation is Windows-only")
    with TemporaryDirectory() as raw:
        outside = Path(raw) / "outside"
        root = Path(raw) / "repo"
        outside.mkdir()
        root.mkdir()
        (outside / "outside.py").write_text("OUTSIDE = True\n", encoding="utf-8")
        (root / "real.py").write_text("REAL = True\n", encoding="utf-8")
        link = root / "linked_dir"
        created = subprocess.run(
            [
                "cmd",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(outside),
            ],
            capture_output=True,
        )
        if created.returncode != 0:
            pytest.skip("junction creation is not permitted on this platform")
        assert os.path.isjunction(link)

        files = sorted(
            path.relative_to(root).as_posix()
            for path in _python_files(root)
        )

        assert files == ["real.py"]


def test_python_files_matches_python_suffix_case_insensitively() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "upper.PY").write_text(
            "def render_test_report(report):\n    return 'upper'\n",
            encoding="utf-8",
        )

        files = [path.relative_to(root).as_posix() for path in _python_files(root)]
        definitions = _collect_definitions(root)
        locations = sorted(
            {
                path.relative_to(root).as_posix()
                for path in definitions["render_test_report"]
            }
        )

        # Windows treats ``.PY`` as Python source; the enum and the AST
        # uniqueness scanner must see it exactly like ``.py``.
        assert files == ["upper.PY"]
        assert locations == ["upper.PY"]


def _collect_definitions(root: Path) -> dict[str, list[Path]]:
    """Collect every FunctionDef/AsyncFunctionDef/ClassDef name per file."""

    definitions: dict[str, list[Path]] = {}
    for path in _python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                definitions.setdefault(node.name, []).append(path)
    return definitions


def test_core_responsibility_organizes_user_provided_materials() -> None:
    frontmatter, body = _load_workflow()

    assert "existing test materials" in frontmatter["description"]
    assert "已有测试材料" in body
    assert "规范 Markdown 测试报告" in body
    assert "只读取用户明确指定的材料" in body
    assert "不做无范围搜索" in body
    for material in (
        "需求/测试范围",
        "环境与版本",
        "测试用例",
        "执行记录",
        "pytest/JUnit/Jenkins 摘要",
        "缺陷清单",
        "日志摘要",
        "制品定位",
        "已有报告",
        "其他用户指定材料",
    ):
        assert material in body


def test_no_jenkins_run_id_build_or_commit_requirement() -> None:
    _, body = _load_workflow()

    assert "不要求材料来自 Jenkins" in body
    assert "不要求用户提供 run ID、build number 或 commit" in body
    assert "workflows/test-reporting/outputs/" in body
    assert "分配当前 report/run 标识" in body
    assert "或使用用户指定路径" in body


def test_missing_conflicting_unreadable_materials_marked_honestly() -> None:
    _, body = _load_workflow()

    assert "缺失、冲突或不可读" in body
    assert "如实标记" in body
    assert "不编造" in body
    assert "未提供的缺陷" in body
    assert "无缺陷" in body
    assert "证据不足" in body
    assert "测试通过" in body


def test_jenkins_junit_optional_input_keeps_evidence_classification() -> None:
    _, body = _load_workflow()
    section = _optional_capability_section(body)

    assert "可选输入" in body
    assert "构建状态与测试结论分离" in section
    for token in (
        "SUCCESS",
        "TESTS_PASSED",
        "TESTS_FAILED",
        "TEST_EXECUTION_INCOMPLETE",
        "TEST_RESULT_UNVERIFIED",
        "TESTS_NOT_EXECUTED",
        "NO_JENKINS_EVIDENCE",
        "零测试或全部跳过",
        "不等于",
    ):
        assert token in section
    assert "至少一个测试实际执行" in section
    assert "并非全部跳过" in section


def test_jenkins_junit_classification_precedes_markdown_rendering() -> None:
    _, body = _load_workflow()
    general = body.split(OPTIONAL_CAPABILITY_HEADING, 1)[0]

    assert "统一分类器分类" in general
    assert "渲染 Markdown" in general
    assert general.index("统一分类器分类") < general.index("渲染 Markdown")


def test_standalone_markdown_output_and_optional_report_context() -> None:
    _, body = _load_workflow()

    assert "独立输出 Markdown" in body
    assert "ReportContext" in body
    assert "来源字段" in body


def test_template_keeps_exact_nine_sections() -> None:
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 报告基本信息",
        "## 测试目标与范围",
        "## 测试环境与版本",
        "## 材料清单与来源",
        "## 执行汇总",
        "## 详细结果/失败项",
        "## 缺陷汇总",
        "## 结论",
        "## 风险、限制与缺失信息",
    ]
    assert "<rules-version>" not in text


def test_workflow_version_bumped_for_changed_contract() -> None:
    frontmatter, _ = _load_workflow()

    assert frontmatter["metadata"]["workflow-version"] == "0.3.0"


def test_report_authority_names_defined_once_repo_wide_by_ast() -> None:
    definitions = _collect_definitions(ROOT)
    for name, expected in _REPORT_AUTHORITY_DEFINITIONS.items():
        locations = sorted(
            {
                path.relative_to(ROOT).as_posix()
                for path in definitions.get(name, [])
            }
        )
        assert locations == [expected], name


def test_legacy_literal_definition_scan_misses_legal_ast_forms() -> None:
    double_space = "def  render_test_report(report):\n    return ''\n"
    async_form = "async def render_test_report(report):\n    return report.run_id\n"

    # The retired literal scanner looked for ``def render_test_report`` and is
    # blind to the legal double-space FunctionDef form.
    assert _LEGACY_DEF_SUBSTRING not in double_space

    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "probe_double.py").write_text(double_space, encoding="utf-8")
        (root / "probe_async.py").write_text(async_form, encoding="utf-8")
        definitions = _collect_definitions(root)
        locations = sorted(
            {
                path.relative_to(root).as_posix()
                for path in definitions["render_test_report"]
            }
        )
        assert locations == ["probe_async.py", "probe_double.py"]


def test_legacy_src_only_scope_misses_workflow_script_second_renderer() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        script = (
            root / "workflows" / "sample-workflow" / "scripts" / "renderer.py"
        )
        script.parent.mkdir(parents=True)
        script.write_text(
            "def render_test_report(report):\n    return 'second renderer'\n",
            encoding="utf-8",
        )
        legacy_hits = [
            path.relative_to(root).as_posix()
            for path in _python_files(root)
            if path.relative_to(root).as_posix().startswith(_LEGACY_SCOPE_PREFIX)
            and _LEGACY_DEF_SUBSTRING in path.read_text(encoding="utf-8")
        ]
        assert legacy_hits == []
        definitions = _collect_definitions(root)
        locations = sorted(
            {
                path.relative_to(root).as_posix()
                for path in definitions["render_test_report"]
            }
        )
        assert locations == ["workflows/sample-workflow/scripts/renderer.py"]


def test_docs_role_and_template_declare_single_python_authority() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    role = ROLE_PATH.read_text(encoding="utf-8")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    _, root_body = parse_markdown(ROOT / "SKILL.md")

    assert "agent_workflow_hub.test_reporting" in skill
    assert "唯一" in skill
    assert "render_test_report" in skill
    assert "report_sha256" in skill
    assert "扩展行" in skill
    assert "agent_workflow_hub.test_reporting" in role
    assert "扩展行" in template
    assert "ReportContext" in template
    row = next(
        line
        for line in root_body.splitlines()
        if line.startswith("| `test-reporting` |")
    )
    assert "agent_workflow_hub.test_reporting" in row


def test_role_and_root_catalogue_match_general_responsibility() -> None:
    _, root_body = parse_markdown(ROOT / "SKILL.md")

    row = next(
        line
        for line in root_body.splitlines()
        if line.startswith("| `test-reporting` |")
    )
    assert "catalogued workflow" in row.lower()
    assert "existing test materials" in row
    assert "Jenkins/JUnit" in row
    assert "optional input" in row

    role = ROLE_PATH.read_text(encoding="utf-8")
    assert "已有测试材料" in role
    assert "如实标记" in role
    assert "不编造" in role
    assert "Jenkins/JUnit 是可选材料" in role
    assert "构建状态与测试结论必须分离" in role
