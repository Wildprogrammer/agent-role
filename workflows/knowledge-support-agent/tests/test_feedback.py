from __future__ import annotations

from pathlib import Path

import pytest

from agent_workflow_hub.knowledge_support_agent.feedback import (
    ConfirmedExperience,
    FeedbackError,
    store_confirmed_experience,
)
from agent_workflow_hub.knowledge_support_agent.store import (
    InMemoryTableBackend,
    KnowledgeStore,
)


def store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(
        (tmp_path / "knowledge-support" / "lancedb").resolve(),
        backend=InMemoryTableBackend(),
    )


def experience(
    answer: str,
    *,
    supersedes: str | None = None,
    confirmed=True,
    scope="ExamplePortal 登录",
):
    return ConfirmedExperience(
        experience_id="login-lock-policy",
        question="连续登录失败后如何处理？",
        answer=answer,
        scope=scope,
        confirmed=confirmed,
        confirmed_at="2026-09-01T10:00:00+08:00",
        supersedes=supersedes,
    )


def test_confirmed_experience_is_versioned_and_immediately_searchable(
    tmp_path: Path,
) -> None:
    target = store(tmp_path)

    record = store_confirmed_experience(target, experience("等待五分钟后重试。"))
    result = target.search("登录失败", query_vector=None)

    assert record.record_id == "login-lock-policy:v1"
    assert record.version == 1
    assert record.active is True
    assert result.evidence[0]["content"] == "等待五分钟后重试。"
    assert result.evidence[0]["source_kind"] == "user-confirmed-experience"
    assert result.evidence[0]["provenance"]["experience_version"] == 1


def test_correction_must_supersede_active_version_and_old_is_not_retrieved(
    tmp_path: Path,
) -> None:
    target = store(tmp_path)
    first = store_confirmed_experience(target, experience("等待五分钟后重试。"))

    with pytest.raises(FeedbackError, match="supersedes"):
        store_confirmed_experience(target, experience("等待十分钟后重试。"))

    second = store_confirmed_experience(
        target,
        experience("等待十分钟后重试。", supersedes=first.record_id),
    )
    result = target.search("登录失败", query_vector=None)

    assert second.record_id == "login-lock-policy:v2"
    assert second.supersedes == first.record_id
    rows = target.experience_rows()
    assert [row["active"] for row in rows] == [False, True]
    assert [item["content"] for item in result.evidence] == ["等待十分钟后重试。"]


def test_unconfirmed_answer_is_rejected_without_a_confirmation_protocol(
    tmp_path: Path,
) -> None:
    target = store(tmp_path)

    with pytest.raises(FeedbackError, match="confirmed"):
        store_confirmed_experience(target, experience("未经确认", confirmed=False))

    assert target.experience_rows() == ()


def test_identical_replay_is_idempotent(tmp_path: Path) -> None:
    target = store(tmp_path)
    value = experience("等待五分钟后重试。")

    first = store_confirmed_experience(target, value)
    replay = store_confirmed_experience(target, value)

    assert replay == first
    assert len(target.experience_rows()) == 1


def test_duplicate_answer_reuses_version_and_updates_scope(tmp_path: Path) -> None:
    target = store(tmp_path)
    first = store_confirmed_experience(target, experience("等待五分钟后重试。"))

    updated = store_confirmed_experience(
        target,
        experience("等待五分钟后重试。", scope="全部内部登录系统"),
    )

    assert updated.record_id == first.record_id
    assert updated.version == first.version
    assert updated.scope == "全部内部登录系统"
    assert len(target.experience_rows()) == 1
    assert target.search("登录", query_vector=None).evidence[0]["section"] == "全部内部登录系统"


class FailingExperienceBackend(InMemoryTableBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_experience = False

    def replace(self, name, rows) -> None:
        if name == "experience" and self.fail_experience:
            raise RuntimeError("experience write failed")
        super().replace(name, rows)


def test_feedback_publish_failure_keeps_previous_searchable_version(
    tmp_path: Path,
) -> None:
    backend = FailingExperienceBackend()
    target = KnowledgeStore(
        (tmp_path / "knowledge-support" / "lancedb").resolve(),
        backend=backend,
    )
    first = store_confirmed_experience(target, experience("等待五分钟后重试。"))
    backend.fail_experience = True

    with pytest.raises(RuntimeError, match="experience write failed"):
        store_confirmed_experience(
            target,
            experience("等待十分钟后重试。", supersedes=first.record_id),
        )

    assert [row["active"] for row in target.experience_rows()] == [True]
    evidence = target.search("登录失败", query_vector=None).evidence
    assert [item["content"] for item in evidence] == ["等待五分钟后重试。"]


class RejectKnowledgeRewriteBackend(InMemoryTableBackend):
    def __init__(self) -> None:
        super().__init__()
        self.reject_knowledge = False

    def replace(self, name, rows) -> None:
        if name == "knowledge" and self.reject_knowledge:
            raise RuntimeError("knowledge rewrite must not be used")
        super().replace(name, rows)


def test_feedback_metadata_is_the_single_searchable_publication(
    tmp_path: Path,
) -> None:
    backend = RejectKnowledgeRewriteBackend()
    target = KnowledgeStore(
        (tmp_path / "knowledge-support" / "lancedb").resolve(),
        backend=backend,
    )
    first = store_confirmed_experience(target, experience("等待五分钟后重试。"))
    backend.reject_knowledge = True

    second = store_confirmed_experience(
        target,
        experience("等待十分钟后重试。", supersedes=first.record_id),
    )

    assert second.record_id == "login-lock-policy:v2"
    evidence = target.search("登录失败", query_vector=None).evidence
    assert [item["content"] for item in evidence] == ["等待十分钟后重试。"]
