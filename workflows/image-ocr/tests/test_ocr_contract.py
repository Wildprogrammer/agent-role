import json
from pathlib import Path

from agent_workflow_hub.contracts import validate_capability, validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_SKILL = REPOSITORY_ROOT / "workflows" / "image-ocr" / "SKILL.md"
UMIOCR_CAPABILITY = REPOSITORY_ROOT / "capabilities" / "cli" / "umi-ocr" / "CAPABILITY.md"
PADDLE_CAPABILITY = REPOSITORY_ROOT / "capabilities" / "model" / "paddleocr" / "CAPABILITY.md"
TESSERACT_CAPABILITY = REPOSITORY_ROOT / "capabilities" / "cli" / "tesseract" / "CAPABILITY.md"


def test_image_ocr_contracts_are_local_only_and_user_managed():
    frontmatter, body = parse_markdown(WORKFLOW_SKILL)
    skill = validate_skill(WORKFLOW_SKILL, frontmatter, body)

    assert skill.name == "image-ocr"
    assert json.loads(frontmatter["metadata"]["required-capabilities"]) == ["cli.umi-ocr"]
    for phrase in (
        "Umi-OCR",
        "PaddleOCR-json",
        "不操作 GUI",
        "云端 OCR",
        "无法可靠识别",
        "不自动下载模型",
        "仅当用户明确要求时写入 `.txt` 或 `.md`",
        "系统修改与权限影响",
        "渐进式只读发现",
        "UmiOCR-data/plugins/win7_x64_PaddleOCR-json",
    ):
        assert phrase in body
    assert "不扫描磁盘寻找安装位置" not in body

    for path, identifier in (
        (UMIOCR_CAPABILITY, "cli.umi-ocr"),
        (PADDLE_CAPABILITY, "model.paddleocr"),
        (TESSERACT_CAPABILITY, "cli.tesseract"),
    ):
        capability_frontmatter, capability_body = parse_markdown(path)
        contract = validate_capability(path, capability_frontmatter, capability_body)

        assert contract.id == identifier
        assert capability_frontmatter["installation"]["policy"] == "user-managed"
        assert capability_frontmatter["network"]["required_for_core_use"] is False
