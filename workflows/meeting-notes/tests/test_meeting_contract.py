from pathlib import Path

from agent_workflow_hub.contracts import validate_capability, validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


EXPECTED = {"cli.ffmpeg", "model.funasr", "app.obsidian"}
FUNASR_LOCK = Path("workflows/meeting-notes/references/funasr-windows-py312.lock")
VOXCPM_LOCK = Path("workflows/meeting-notes/references/voxcpm-windows-py312.lock")


def test_meeting_capability_contracts_exist():
    found = set()
    for path in Path("capabilities").glob("*/*/CAPABILITY.md"):
        contract = validate_capability(path, *parse_markdown(path))
        if contract.id in EXPECTED:
            found.add(contract.id)

    assert found == EXPECTED


def test_funasr_is_local_by_default():
    frontmatter, _ = parse_markdown(
        Path("capabilities/model/funasr/CAPABILITY.md")
    )

    assert frontmatter["data_policy"] == "local-default"
    assert frontmatter["cloud_upload"] == "explicit-opt-in-only"


def test_funasr_agent_installation_uses_the_hashed_windows_py312_lock():
    frontmatter, body = parse_markdown(Path("capabilities/model/funasr/CAPABILITY.md"))
    lock = FUNASR_LOCK.read_text(encoding="utf-8")

    assert frontmatter["installation"] == {
        "policy": "agent-managed",
        "scope": "global-runtime",
        "methods": ["existing", "pip", "uv"],
    }
    assert "funasr-windows-py312.lock" in body
    assert "--require-hashes" in body
    assert "<HUB_ROOT>\\workspace\\shared\\runtimes\\funasr-py312" in body
    assert "%LOCALAPPDATA%\\agent-workflow-hub\\runtimes\\funasr-py312" in body
    assert "legacy location" in body.lower()
    assert "pre-populated Python" in body
    assert "funasr==1.3.14" in lock
    assert "--hash=sha256:" in lock


def test_meeting_skill_allows_only_the_locked_funasr_runtime_install():
    _, body = parse_markdown(Path("workflows/meeting-notes/SKILL.md"))

    assert "funasr-windows-py312.lock" in body
    assert "模型权重仍不自动下载" in body
    assert "不会自行安装 FunASR" not in body


def test_meeting_skill_contains_privacy_branches():
    path = Path("workflows/meeting-notes/SKILL.md")
    contract = validate_skill(path, *parse_markdown(path))
    body = contract.body

    assert "仅保留转写" in body
    assert "不生成 meeting-notes.md" in body
    assert "不写入 Obsidian" in body
    assert "参与者知情" in body
    assert "不得推断负责人" in body


def test_voxcpm_is_optional_and_uses_the_hashed_windows_py312_lock():
    frontmatter, body = parse_markdown(Path("capabilities/model/voxcpm/CAPABILITY.md"))
    lock = VOXCPM_LOCK.read_text(encoding="utf-8")

    assert frontmatter["id"] == "model.voxcpm"
    assert frontmatter["installation"] == {
        "policy": "agent-managed",
        "scope": "global-runtime",
        "methods": ["existing", "pip", "uv"],
    }
    assert frontmatter["network"]["required_for_core_use"] is False
    assert "voxcpm-windows-py312.lock" in body
    assert "dry-run" in body
    assert "--require-hashes" in body
    assert "<HUB_ROOT>\\workspace\\shared\\runtimes\\voxcpm-py312" in body
    assert "%LOCALAPPDATA%\\agent-workflow-hub\\runtimes\\voxcpm-py312" in body
    assert "legacy location" in body.lower()
    assert "voxcpm==2.0.3" in lock
    assert "--hash=sha256:" in lock
    assert "不得启动局域网服务" in body
    assert "授权记录" not in body
    assert "合成/克隆许可" not in body


def test_voxcpm_lock_pins_the_windows_cuda_wheels():
    lock = VOXCPM_LOCK.read_text(encoding="utf-8")

    assert "torch==2.5.1+cu121" in lock
    assert "torchaudio==2.5.1+cu121" in lock
    assert "--index-strategy unsafe-best-match" in lock


def test_meeting_skill_keeps_voxcpm_out_of_required_core_capabilities():
    frontmatter, body = parse_markdown(Path("workflows/meeting-notes/SKILL.md"))

    assert "model.voxcpm" not in frontmatter["metadata"]["required-capabilities"]
    assert "声音克隆" in body
    assert "会议录音" in body


def test_meeting_skill_documents_real_commands_and_optional_voice_branch():
    _, body = parse_markdown(Path("workflows/meeting-notes/SKILL.md"))

    assert "meeting-notes transcribe" in body
    assert "meeting-notes speak" in body
    assert "当前 Agent" in body
    assert "不会主动上传原始会议录音文件" in body
    assert "outputs/<run-id>/" in body
    assert "--clone --reference-audio" in body
    assert "Gate V" not in body
    assert "--clone-consent-file" not in body
    assert "保留期限" not in body
    assert "3–15 秒" in body
    assert "单一说话人" in body
    assert "质量建议" in body
