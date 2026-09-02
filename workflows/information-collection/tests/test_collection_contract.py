import json
from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_SKILL = REPOSITORY_ROOT / "workflows" / "information-collection" / "SKILL.md"


def test_information_collection_skill_declares_safe_first_phase_boundaries():
    frontmatter, body = parse_markdown(WORKFLOW_SKILL)
    contract = validate_skill(WORKFLOW_SKILL, frontmatter, body)

    assert contract.name == "information-collection"
    assert json.loads(frontmatter["metadata"]["required-capabilities"]) == []
    assert json.loads(frontmatter["metadata"]["supported-hosts"]) == [
        "codex", "openclaw", "claude-code", "hermes", "opencode"
    ]
    for rule in (
        "多个独立的采集任务",
        "最多重试两次",
        "markdown-converter",
        "python-docx",
        "宿主原生网页读取",
        "Tavily",
        "自动降级",
        "部分成功",
        "%USERPROFILE%/.openclaw/skills",
        "普通解析模块",
        "中风险",
        "手动任务",
        "已批准的定时任务",
        "全部失败",
        "git clone",
        "playwright-cli",
        "agent-browser",
        "TAVILY_API_KEY",
        "--topic news",
        "--deep",
        "extract.mjs",
        "final URL",
    ):
        assert rule in contract.body

    for removed in (
        "验证码",
        "付费墙",
        "Cookie、凭据、会话令牌",
        "CloakBrowser",
        "代理、指纹、UA、语言、视窗和端点轮换",
    ):
        assert removed not in contract.body

    for heading in (
        "用途与触发条件",
        "非目标",
        "输入",
        "输出与命名规则",
        "依赖和运行前检查",
        "系统修改与权限影响",
        "执行步骤",
        "人工确认门",
        "失败恢复",
        "重跑、幂等与覆盖策略",
        "验收标准",
        "清理方式",
    ):
        assert heading in contract.body


def test_information_collection_browser_and_tavily_guidance_preserves_operational_bounds():
    _frontmatter, body = parse_markdown(WORKFLOW_SKILL)

    for rule in (
        "已安装的 Python 或 Node Playwright",
        "当选定的 Playwright 路径不可用",
        "当前任务明确允许第三方云端搜索或提取",
        "`-n 5`",
        "npx skills add https://github.com/tavily-ai/skills --skill tavily-search",
        "tvly login",
        "Test-Path Env:TAVILY_API_KEY",
        "setx TAVILY_API_KEY",
        "不得通过对话、仓库文件或运行记录接收 API Key",
        "发现线索而非最终事实依据",
        "缺少 Playwright 或 `playwright-cli` 时，先向用户询问是否安装",
        "对应能力的 `installation` 合约",
        "浏览器内核仍需单独的高风险确认",
    ):
        assert rule in body


def test_information_collection_routing_layers_are_ordered_and_scope_rules_bounded():
    _frontmatter, body = parse_markdown(WORKFLOW_SKILL)

    direct = body.index("第一层：直接来源")
    tavily = body.index("第二层：Tavily")
    browser = body.index("第三层：自动化浏览器")
    assert direct < tavily < browser

    for required in (
        "官方 API",
        "RSS",
        "公开 JSON",
        "普通 URL 直接只读读取",
        "Playwright 为主",
        "`agent-browser` 为同层备选",
        "默认只采集该页面（exact-page）",
        "follow_same_domain",
        "规范化 hostname",
        "忽略 scheme 与端口",
        "跨域永不自动扩展",
        "重定向跨域拒绝",
    ):
        assert required in body

    for removed in (
        "Tavily、HTTP",
        "HTTP 或浏览器路径",
    ):
        assert removed not in body
