from __future__ import annotations

from dataclasses import dataclass
import re
from itertools import zip_longest
from typing import Mapping, Protocol

from .client import JenkinsClientError, JenkinsItem, JenkinsPlugin, JenkinsView
from .models import OperationRequest, WritePermit
from .service import OperationPolicyService
from .templates import PluginRequirement, TemplateError, render_template


class ItemOperationError(RuntimeError):
    pass


class _ItemClient(Protocol):
    def get_plugin_snapshot(self) -> tuple[JenkinsPlugin, ...]: ...

    def get_item(self, item_path: str) -> JenkinsItem: ...

    def get_view(self, parent_path: str | None, name: str) -> JenkinsView: ...

    def _create_item(
        self,
        parent_path: str | None,
        name: str,
        item_type: str,
        template: str,
        permit: WritePermit,
        parameters: Mapping[str, str] | None = None,
    ) -> None: ...

    def _create_view(
        self,
        parent_path: str | None,
        name: str,
        template: str,
        permit: WritePermit,
    ) -> None: ...


@dataclass(frozen=True)
class CreateItemRequest:
    controller: str
    item_path: str
    item_type: str
    template: str
    parameters: Mapping[str, str] | None = None


class JenkinsItemService:
    def __init__(self, client: _ItemClient, policy: OperationPolicyService) -> None:
        self._client = client
        self._policy = policy

    def create(
        self,
        request: CreateItemRequest,
        *,
        permit: WritePermit,
    ) -> JenkinsItem | JenkinsView:
        operation = prepare_create_operation(request)
        item_path, parent_path, name = _split_item_path(operation.item_path)
        parameters = dict(operation.parameters)
        try:
            rendered = render_template(
                item_type=request.item_type,
                template=request.template,
                parameters=parameters,
            )
        except TemplateError as exc:
            raise ItemOperationError(str(exc)) from None
        if self._policy.inspect_write_permit(permit) != operation:
            raise ItemOperationError("create_item denied: write authorization is invalid")
        if rendered.required_plugins:
            _require_plugins(self._client.get_plugin_snapshot(), rendered.required_plugins)
        self._ensure_missing_before_creation(
            item_type=request.item_type,
            item_path=item_path,
            parent_path=parent_path,
            name=name,
        )
        try:
            if request.item_type == "view":
                self._client._create_view(parent_path, name, request.template, permit)
                created: JenkinsItem | JenkinsView = self._client.get_view(parent_path, name)
            else:
                self._client._create_item(
                    parent_path,
                    name,
                    request.item_type,
                    request.template,
                    permit,
                    parameters=parameters,
                )
                created = self._client.get_item(item_path)
        except JenkinsClientError:
            raise ItemOperationError("Jenkins item creation did not complete with readable evidence") from None
        if request.item_type == "view":
            if not isinstance(created, JenkinsView) or created.parent_path != parent_path or created.name != name:
                raise ItemOperationError("Jenkins view readback does not match the requested view")
        elif not isinstance(created, JenkinsItem) or created.item_path != item_path:
            raise ItemOperationError("Jenkins item readback path does not match the requested item")
        if created.jenkins_class != rendered.expected_jenkins_class:
            raise ItemOperationError("Jenkins item readback type does not match the requested template")
        return created

    def _ensure_missing_before_creation(
        self,
        *,
        item_type: str,
        item_path: str,
        parent_path: str | None,
        name: str,
    ) -> None:
        try:
            if item_type == "view":
                self._client.get_view(parent_path, name)
            else:
                self._client.get_item(item_path)
        except JenkinsClientError as exc:
            if exc.kind == "http_status" and exc.status_code == 404:
                return
            raise ItemOperationError("could not determine whether the Jenkins item already exists") from None
        raise ItemOperationError("Jenkins item already exists")


def prepare_create_operation(request: CreateItemRequest) -> OperationRequest:
    item_path, _, _ = _split_item_path(request.item_path)
    if not isinstance(request.parameters, (Mapping, type(None))) or any(
        not isinstance(name, str) or not name or not isinstance(value, str)
        for name, value in (request.parameters or {}).items()
    ):
        raise ItemOperationError("parameters must be a mapping of strings")
    parameters = dict(request.parameters or {})
    try:
        render_template(
            item_type=request.item_type,
            template=request.template,
            parameters=parameters,
        )
    except TemplateError as exc:
        raise ItemOperationError(str(exc)) from None
    return OperationRequest(
        controller=request.controller,
        action="create_item",
        item_path=item_path,
        item_type=request.item_type,
        template=request.template,
        parameters=parameters,
    )


def _split_item_path(item_path: str) -> tuple[str, str | None, str]:
    if not isinstance(item_path, str) or not item_path or item_path.startswith(("/", "\\")):
        raise ItemOperationError("invalid Jenkins item path")
    parts = item_path.split("/")
    if any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise ItemOperationError("invalid Jenkins item path")
    return "/".join(parts), "/".join(parts[:-1]) or None, parts[-1]


def _require_plugins(
    plugins: tuple[JenkinsPlugin, ...],
    required: tuple[PluginRequirement, ...],
) -> None:
    available = {plugin.short_name: plugin for plugin in plugins}
    unavailable: list[str] = []
    for requirement in required:
        plugin = available.get(requirement.short_name)
        if plugin is None or plugin.active is not True or plugin.version is None:
            unavailable.append(requirement.short_name)
            continue
        if not _jenkins_version_at_least(plugin.version, requirement.minimum_version):
            unavailable.append(requirement.short_name)
    if unavailable:
        raise ItemOperationError(
            f"required Jenkins plugin is unavailable or incompatible: {', '.join(sorted(unavailable))}"
        )


def _jenkins_version_at_least(actual: str, minimum: str) -> bool:
    actual_match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.v[A-Za-z0-9_]+)?", actual)
    minimum_match = re.fullmatch(r"\d+(?:\.\d+)*", minimum)
    if actual_match is None or minimum_match is None:
        return False
    actual_parts = tuple(int(part) for part in actual_match.group(1).split("."))
    minimum_parts = tuple(int(part) for part in minimum.split("."))
    for actual_part, minimum_part in zip_longest(actual_parts, minimum_parts, fillvalue=0):
        if actual_part != minimum_part:
            return actual_part > minimum_part
    return True
