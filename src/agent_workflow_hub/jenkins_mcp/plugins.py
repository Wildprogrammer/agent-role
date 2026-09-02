from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .client import ControllerInfo, JenkinsItem, JenkinsJUnitSummary, JenkinsPipelineRun, JenkinsPlugin
from .items import _jenkins_version_at_least
from .templates import PluginRequirement


class PluginCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginDriver:
    name: str
    requirements: tuple[PluginRequirement, ...]
    maximum_plugin_version: str
    minimum_jenkins_version: str
    maximum_jenkins_version: str | None
    required_permissions: frozenset[str]
    supported_fields: frozenset[str]
    risk_level: str
    fixture_name: str


class PluginDriverCatalog:
    """A closed catalog prevents dynamically invoking arbitrary Jenkins plugins."""

    _DRIVERS = {
        "pipeline_rest": PluginDriver(
            name="pipeline_rest",
            requirements=(PluginRequirement("pipeline-rest-api", "2.34"),),
            maximum_plugin_version="2.34",
            minimum_jenkins_version="2.361.4",
            maximum_jenkins_version=None,
            required_permissions=frozenset({"Overall/Read", "Job/Read"}),
            supported_fields=frozenset({"run_id", "name", "status", "start_time_ms", "end_time_ms", "duration_ms"}),
            risk_level="low",
            fixture_name="workflows/jenkins-operations/tests/fixtures/pipeline-rest-api-runs.json",
        ),
        "junit": PluginDriver(
            name="junit",
            requirements=(PluginRequirement("junit", "1.0"),),
            maximum_plugin_version="1.0",
            minimum_jenkins_version="2.0",
            maximum_jenkins_version=None,
            required_permissions=frozenset({"Overall/Read", "Job/Read"}),
            supported_fields=frozenset({"total_count", "fail_count", "skip_count"}),
            risk_level="low",
            fixture_name="workflows/jenkins-operations/tests/fixtures/junit-summary.json",
        ),
        "multibranch": PluginDriver(
            name="multibranch",
            requirements=(PluginRequirement("workflow-multibranch", "2.0"),),
            maximum_plugin_version="2.0",
            minimum_jenkins_version="2.0",
            maximum_jenkins_version=None,
            required_permissions=frozenset({"Overall/Read", "Job/Read"}),
            supported_fields=frozenset({"item_path", "name", "jenkins_class", "color"}),
            risk_level="low",
            fixture_name="workflows/jenkins-operations/tests/fixtures/multibranch-children.json",
        ),
    }

    def require(
        self,
        name: str,
        plugins: tuple[JenkinsPlugin, ...],
        controller_version: str | None = None,
    ) -> PluginDriver:
        driver = self._DRIVERS.get(name)
        if driver is None:
            raise PluginCapabilityError("unknown Jenkins plugin capability")
        if (
            controller_version is None
            or not _jenkins_version_at_least(controller_version, driver.minimum_jenkins_version)
            or (
                driver.maximum_jenkins_version is not None
                and not _jenkins_version_at_least(driver.maximum_jenkins_version, controller_version)
            )
        ):
            raise PluginCapabilityError("required Jenkins plugin capability is unavailable")
        available = {plugin.short_name: plugin for plugin in plugins}
        for requirement in driver.requirements:
            plugin = available.get(requirement.short_name)
            if (
                plugin is None
                or plugin.active is not True
                or plugin.version is None
                or not _jenkins_version_at_least(plugin.version, requirement.minimum_version)
            ):
                raise PluginCapabilityError("required Jenkins plugin capability is unavailable")
        return driver


class _PluginReadClient(Protocol):
    def get_plugin_snapshot(self) -> tuple[JenkinsPlugin, ...]: ...

    def get_controller_info(self) -> ControllerInfo: ...

    def _get_pipeline_runs(self, item_path: str) -> tuple[JenkinsPipelineRun, ...]: ...

    def _get_junit_summary(self, item_path: str, number: int) -> JenkinsJUnitSummary: ...

    def get_item(self, item_path: str) -> JenkinsItem: ...

    def _list_child_items(self, item_path: str) -> tuple[JenkinsItem, ...]: ...


class JenkinsPluginReadService:
    """Read plugin endpoints only after the matching closed-catalog driver is available."""

    def __init__(self, client: _PluginReadClient, catalog: PluginDriverCatalog | None = None) -> None:
        self._client = client
        self._catalog = catalog or PluginDriverCatalog()

    def pipeline_runs(self, item_path: str) -> tuple[JenkinsPipelineRun, ...]:
        self._require("pipeline_rest")
        return self._client._get_pipeline_runs(item_path)

    def junit_summary(self, item_path: str, number: int) -> JenkinsJUnitSummary:
        self._require("junit")
        return self._client._get_junit_summary(item_path, number)

    def require_multibranch(self) -> PluginDriver:
        return self._require("multibranch")

    def multibranch_children(self, item_path: str) -> tuple[JenkinsItem, ...]:
        self._require("multibranch")
        parent = self._client.get_item(item_path)
        if parent.jenkins_class != "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject":
            raise PluginCapabilityError("requested item is not a supported multibranch pipeline")
        children = self._client._list_child_items(item_path)
        expected_prefix = f"{item_path}/"
        for child in children:
            if (
                not child.item_path.startswith(expected_prefix)
                or child.item_path.count("/") != item_path.count("/") + 1
                or child.jenkins_class != "org.jenkinsci.plugins.workflow.job.WorkflowJob"
            ):
                raise PluginCapabilityError("multibranch response contains an unsupported child item")
        return children

    def _require(self, driver_name: str) -> PluginDriver:
        plugins = self._client.get_plugin_snapshot()
        controller = self._client.get_controller_info()
        return self._catalog.require(driver_name, plugins, controller.version)
