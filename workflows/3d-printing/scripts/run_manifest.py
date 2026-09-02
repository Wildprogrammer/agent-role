from __future__ import annotations

from dataclasses import dataclass, field


class StageError(ValueError):
    pass


@dataclass
class RunManifest:
    run_id: str
    status: str = "in_progress"
    constraints_confirmed: bool = False
    model_path: str | None = None
    validation_path: str | None = None
    validation_passed: bool = False
    slicer_provider: str | None = None
    slicer_profile: str | None = None
    slicer_confirmed: bool = False
    gcode_path: str | None = None
    final_review_confirmed: bool = False
    events: list[str] = field(default_factory=list)

    def confirm_constraints(self) -> None:
        self.constraints_confirmed = True
        self.events.append("gate-a-confirmed")

    def record_model(self, path: str) -> None:
        if not self.constraints_confirmed:
            raise StageError("manufacturing constraints confirmation required")
        self.model_path = path
        self.events.append("model-recorded")

    def record_validation(self, path: str, *, passed: bool) -> None:
        if not self.model_path:
            raise StageError("model required")
        self.validation_path = path
        self.validation_passed = passed
        self.events.append(f"validation-{'passed' if passed else 'failed'}")

    def confirm_slicer(self, provider: str, profile: str) -> None:
        if not self.validation_passed:
            raise StageError("passed validation required")
        self.slicer_provider = provider
        self.slicer_profile = profile
        self.slicer_confirmed = True
        self.events.append(f"gate-b-confirmed:{provider}:{profile}")

    def switch_slicer(self, provider: str) -> None:
        self.slicer_provider = provider
        self.slicer_profile = None
        self.slicer_confirmed = False
        self.gcode_path = None
        self.final_review_confirmed = False
        self.events.append(f"slicer-switched:{provider}")

    def record_gcode(self, path: str) -> None:
        if not self.slicer_confirmed:
            raise StageError("slicer confirmation required")
        self.gcode_path = path
        self.events.append("gcode-recorded")

    def confirm_final_review(self) -> None:
        if not self.gcode_path:
            raise StageError("gcode required")
        self.final_review_confirmed = True
        self.events.append("gate-c-confirmed")

    def deliver(self) -> None:
        if not self.final_review_confirmed:
            raise StageError("final review required")
        self.status = "done"
        self.events.append("delivered")
