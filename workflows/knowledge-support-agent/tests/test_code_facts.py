from __future__ import annotations

from agent_workflow_hub.knowledge_support_agent.code_facts import extract_code_facts


SHA = "a" * 40


def test_python_test_case_fact_keeps_documented_purpose() -> None:
    facts = extract_code_facts(
        source_id="repo",
        path="tests/test_login.py",
        content=(
            b'def test_invalid_password(client):\n'
            b'    """\xe6\x8b\x92\xe7\xbb\x9d\xe9\x94\x99\xe8\xaf\xaf\xe5\xaf\x86\xe7\xa0\x81\xe3\x80\x82"""\n'
            b'    pass\n'
        ),
        commit_sha=SHA,
    )

    assert facts[0].title == "test_invalid_password"
    assert facts[0].content == "拒绝错误密码。"
    assert facts[0].provenance["symbol_type"] == "function"
    assert facts[0].provenance["summary_state"] == "documented"
    assert facts[0].inferred is False


def test_python_undocumented_symbol_does_not_invent_purpose() -> None:
    facts = extract_code_facts(
        source_id="repo",
        path="src/login.py",
        content=b"class LoginService:\n    def authenticate(self, user):\n        pass\n",
        commit_sha=SHA,
    )

    login = next(fact for fact in facts if fact.title == "LoginService")
    authenticate = next(fact for fact in facts if fact.title == "authenticate")
    assert login.content == "class LoginService"
    assert authenticate.content.startswith("def authenticate(")
    assert authenticate.provenance["summary_state"] == "needed"
    assert authenticate.inferred is False


def test_typescript_and_vue_scanner_only_records_declared_symbols() -> None:
    typescript = extract_code_facts(
        source_id="repo",
        path="src/session.ts",
        content=b"export class Session {}\nexport function renew(token: string) {}\n",
        commit_sha=SHA,
    )
    vue = extract_code_facts(
        source_id="repo",
        path="src/Login.vue",
        content=b"<script setup>\nconst submitLogin = async () => {}\n</script>\n",
        commit_sha=SHA,
    )

    assert {fact.title for fact in typescript} == {"Session", "renew"}
    assert {fact.title for fact in vue} == {"submitLogin"}
    assert all(fact.provenance["summary_state"] == "needed" for fact in (*typescript, *vue))


def test_unsupported_or_invalid_code_returns_no_facts() -> None:
    assert extract_code_facts(
        source_id="repo",
        path="src/data.bin",
        content=b"binary",
        commit_sha=SHA,
    ) == ()
    assert extract_code_facts(
        source_id="repo",
        path="src/broken.py",
        content=b"def broken(:",
        commit_sha=SHA,
    ) == ()
