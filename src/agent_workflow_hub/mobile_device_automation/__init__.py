"""Android mobile-device automation contracts and runtimes."""

from .contracts import (
    ContractError,
    load_manifest,
    load_request,
    plan_payload,
    plan_sha256,
)
from .models import (
    AssertionSpec,
    AutomationRequest,
    DeviceSpec,
    ParameterSpec,
    StepSpec,
    ViewportSpec,
)


def cli_main(argv=None):
    """Load the CLI lazily so importing contracts has no runtime side effects."""
    from .cli import main

    return main(argv)

__all__ = [
    "AssertionSpec",
    "AutomationRequest",
    "ContractError",
    "DeviceSpec",
    "ParameterSpec",
    "StepSpec",
    "ViewportSpec",
    "load_manifest",
    "load_request",
    "plan_payload",
    "plan_sha256",
    "cli_main",
]
