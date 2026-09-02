from __future__ import annotations

from collections.abc import Iterable, Mapping, Set

PROJECT_HOSTS = frozenset(
    {"codex", "openclaw", "claude-code", "hermes", "opencode"}
)
VERIFIED = frozenset({"verified"})


def host_compatibility(
    project_hosts: Set[str],
    workflow_hosts: Set[str],
    capability_hosts: Iterable[Mapping[str, str]],
    adapter_states: Mapping[str, str],
) -> dict[str, str]:
    capabilities = tuple(capability_hosts)
    report: dict[str, str] = {}
    for host in sorted(set(project_hosts) & set(workflow_hosts)):
        states = [
            adapter_states.get(host, "unverified"),
            *(matrix.get(host, "unverified") for matrix in capabilities),
        ]
        if "unsupported" in states:
            report[host] = "unsupported"
        elif any(state not in {"verified", "conditional"} for state in states):
            report[host] = "unverified"
        elif "conditional" in states:
            report[host] = "conditional"
        else:
            report[host] = "verified"
    return report


def effective_hosts(
    project: Set[str],
    workflow: Set[str],
    capability_matrices: Iterable[Mapping[str, str]],
    adapters: Mapping[str, str],
) -> set[str]:
    report = host_compatibility(
        project,
        workflow,
        capability_matrices,
        adapters,
    )
    return {host for host, status in report.items() if status in VERIFIED}
