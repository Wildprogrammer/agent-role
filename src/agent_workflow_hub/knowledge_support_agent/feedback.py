"""Versioned, user-confirmed knowledge written directly to the agent database."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime

from .store import KnowledgeStore


class FeedbackError(ValueError):
    """Raised when feedback has not met the business confirmation contract."""


@dataclass(frozen=True, kw_only=True)
class ConfirmedExperience:
    experience_id: str
    question: str
    answer: str
    scope: str
    confirmed: bool
    confirmed_at: str
    supersedes: str | None = None


@dataclass(frozen=True, kw_only=True)
class ExperienceRecord:
    record_id: str
    experience_id: str
    version: int
    question: str
    answer: str
    scope: str
    confirmed_at: str
    supersedes: str | None
    active: bool


def store_confirmed_experience(
    store: KnowledgeStore,
    experience: ConfirmedExperience,
) -> ExperienceRecord:
    _validate(experience)
    rows = [dict(row) for row in store.experience_rows()]
    related = [
        _record(row) for row in rows if row.get("experience_id") == experience.experience_id
    ]
    active = next((record for record in related if record.active), None)
    if active is not None and experience.supersedes is None:
        if active.answer == experience.answer.strip():
            updated = replace(
                active,
                question=experience.question.strip(),
                scope=experience.scope.strip(),
                confirmed_at=experience.confirmed_at,
            )
            if updated == active:
                return active
            rows = [
                asdict(updated) if row.get("record_id") == active.record_id else row
                for row in rows
            ]
            store.replace_experiences(rows)
            return updated
        raise FeedbackError("supersedes must name the active experience version")
    if active is None and experience.supersedes is not None:
        raise FeedbackError("supersedes must be absent for the first version")
    if active is not None and experience.supersedes != active.record_id:
        raise FeedbackError("supersedes must name the active experience version")
    version = max((record.version for record in related), default=0) + 1
    record = ExperienceRecord(
        record_id=f"{experience.experience_id}:v{version}",
        experience_id=experience.experience_id,
        version=version,
        question=experience.question.strip(),
        answer=experience.answer.strip(),
        scope=experience.scope.strip(),
        confirmed_at=experience.confirmed_at,
        supersedes=experience.supersedes,
        active=True,
    )
    for row in rows:
        if row.get("experience_id") == experience.experience_id and row.get("active"):
            row["active"] = False
    rows.append(asdict(record))
    store.replace_experiences(rows)
    return record


def _validate(experience: ConfirmedExperience) -> None:
    if not isinstance(experience, ConfirmedExperience):
        raise FeedbackError("confirmed experience contract is required")
    if experience.confirmed is not True:
        raise FeedbackError("user answer must be confirmed before storage")
    for label, value in (
        ("experience_id", experience.experience_id),
        ("question", experience.question),
        ("answer", experience.answer),
        ("scope", experience.scope),
    ):
        if not isinstance(value, str) or not value.strip():
            raise FeedbackError(f"{label} must be non-empty text")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in experience.experience_id):
        raise FeedbackError("experience_id must be canonical")
    try:
        parsed = datetime.fromisoformat(experience.confirmed_at)
    except (TypeError, ValueError):
        raise FeedbackError("confirmed_at must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise FeedbackError("confirmed_at must include a timezone")


def _record(row: dict[str, object]) -> ExperienceRecord:
    return ExperienceRecord(
        record_id=str(row["record_id"]),
        experience_id=str(row["experience_id"]),
        version=int(row["version"]),
        question=str(row["question"]),
        answer=str(row["answer"]),
        scope=str(row["scope"]),
        confirmed_at=str(row["confirmed_at"]),
        supersedes=(str(row["supersedes"]) if row.get("supersedes") else None),
        active=bool(row["active"]),
    )
