from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, Iterator, Mapping
from urllib.parse import quote, urlsplit

import httpx

from .models import ControllerConfig, JenkinsCredentials, OperationRequest, WritePermit
from .service import OperationPolicyService
from .templates import TemplateError, render_template


@dataclass
class _ConfigUpdateLockEntry:
    lock: Lock
    users: int = 0


_CONFIG_UPDATE_LOCKS: dict[tuple[str, str, str, str], _ConfigUpdateLockEntry] = {}
_CONFIG_UPDATE_LOCKS_GUARD = Lock()
_SENSITIVE_LOG_ASSIGNMENT = re.compile(
    r"(?im)(?P<key>[\"']?(?:[A-Za-z0-9_-]*?(?:token|password|secret|crumb|cookie|key)[A-Za-z0-9_-]*)[\"']?\s*(?:=|:)\s*)(?P<value>\"(?:\\.|[^\"\r\n])*\"|'(?:\\.|[^'\r\n])*'|[^\s,}\]]+)"
)
_SENSITIVE_LOG_HEADER = re.compile(
    r"(?im)(?P<prefix>\b(?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*"
)
_JUNIT_DIAGNOSTIC_KEYS = ("totalCount", "failCount", "skipCount", "suites", "cases")
_JUNIT_SUITE_DIAGNOSTIC_KEYS = ("totalCount", "failCount", "skipCount", "cases")


class JenkinsClientError(RuntimeError):
    def __init__(
        self,
        kind: str,
        *,
        status_code: int | None = None,
        diagnostic: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.diagnostic = diagnostic
        message = f"Jenkins client error: {kind}"
        if status_code is not None:
            message = f"{message} ({status_code})"
        super().__init__(message)


@dataclass(frozen=True)
class ControllerInfo:
    version: str | None
    mode: str | None


@dataclass(frozen=True)
class JenkinsNode:
    name: str
    offline: bool
    temporarily_offline: bool
    num_executors: int
    idle: bool


@dataclass(frozen=True)
class JenkinsItem:
    item_path: str
    name: str
    full_name: str
    jenkins_class: str | None
    web_url: str | None
    color: str | None


@dataclass(frozen=True)
class JenkinsPlugin:
    short_name: str
    version: str | None
    active: bool | None


@dataclass(frozen=True)
class JenkinsView:
    parent_path: str | None
    name: str
    jenkins_class: str | None
    web_url: str | None


@dataclass(frozen=True)
class JenkinsQueueItem:
    queue_id: int
    cancelled: bool
    executable_number: int | None
    why: str | None


@dataclass(frozen=True)
class JenkinsArtifact:
    file_name: str
    display_path: str
    relative_path: str


@dataclass(frozen=True)
class JenkinsBuild:
    number: int
    building: bool
    result: str | None
    timestamp_ms: int
    duration_ms: int
    artifacts: tuple[JenkinsArtifact, ...]


@dataclass(frozen=True)
class JenkinsProgressiveLog:
    text: str
    next_start: int
    more_data: bool


@dataclass(frozen=True)
class JenkinsJUnitSummary:
    total_count: int
    fail_count: int
    skip_count: int


@dataclass(frozen=True)
class JenkinsPipelineRun:
    run_id: str
    name: str
    status: str
    start_time_ms: int
    end_time_ms: int | None
    duration_ms: int


class JenkinsClient:
    """A fixed-endpoint Jenkins Remote API client; it exposes no raw request method."""

    def __init__(
        self,
        controller: ControllerConfig,
        credentials: JenkinsCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        require_crumb: bool | None = None,
        write_authority: OperationPolicyService | None = None,
    ) -> None:
        if write_authority is not None:
            if type(write_authority) is not OperationPolicyService:
                raise TypeError("write_authority must be the trusted OperationPolicyService runtime")
            if not write_authority.controls_controller(controller):
                raise ValueError("write_authority is not bound to this exact controller configuration")
        self._require_crumb = controller.require_crumb if require_crumb is None else require_crumb
        self._crumb: tuple[str, str] | None = None
        self._controller_name = controller.name
        self._controller_url = controller.url.rstrip("/")
        self._controller_url_parts = urlsplit(self._controller_url)
        self._controller_update_lock_key = (controller.name, controller.url, controller.environment)
        self._write_authority = write_authority
        self._log_redaction_lock = Lock()
        self._log_redaction_values: set[str] = {credentials.token}
        verify: bool | str = str(controller.ca_bundle) if controller.ca_bundle else True
        self._client = httpx.Client(
            base_url=f"{controller.url.rstrip('/')}/",
            auth=httpx.BasicAuth(credentials.username, credentials.token),
            timeout=timeout,
            transport=transport,
            verify=verify,
            follow_redirects=False,
        )

    def __enter__(self) -> JenkinsClient:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_controller_info(self) -> ControllerInfo:
        response = self._get("api/json")
        body = self._json_mapping(response)
        return ControllerInfo(version=response.headers.get("X-Jenkins"), mode=_optional_string(body.get("mode")))

    def list_nodes(self) -> tuple[JenkinsNode, ...]:
        response = self._get(
            "computer/api/json",
            params={
                "tree": "computer[displayName,offline,temporarilyOffline,numExecutors,idle]"
            },
        )
        body = self._json_mapping(response)
        computers = body.get("computer")
        if not isinstance(computers, list):
            raise JenkinsClientError("invalid_response")
        return tuple(_node_from_json(computer) for computer in computers)

    def list_items(self) -> tuple[JenkinsItem, ...]:
        response = self._get("api/json", params={"tree": "jobs[name,fullName,_class,url,color]"})
        body = self._json_mapping(response)
        jobs = body.get("jobs")
        if not isinstance(jobs, list):
            raise JenkinsClientError("invalid_response")
        return tuple(_item_from_json(job, item_path=_item_path_from_json(job)) for job in jobs)

    def _list_child_items(self, parent_path: str) -> tuple[JenkinsItem, ...]:
        _, endpoint = _item_endpoint(parent_path)
        response = self._get(f"{endpoint}/api/json", params={"tree": "jobs[name,fullName,_class,url,color]"})
        body = self._json_mapping(response)
        jobs = body.get("jobs")
        if not isinstance(jobs, list):
            raise JenkinsClientError("invalid_response")
        return tuple(_item_from_json(job, item_path=_item_path_from_json(job)) for job in jobs)

    def get_item(self, item_path: str) -> JenkinsItem:
        normalized_path, endpoint = _item_endpoint(item_path)
        response = self._get(
            f"{endpoint}/api/json",
            params={"tree": "name,fullName,_class,url,color"},
        )
        return _item_from_json(self._json_mapping(response), item_path=normalized_path)

    def get_view(self, parent_path: str | None, name: str) -> JenkinsView:
        endpoint = _view_endpoint(parent_path, name)
        response = self._get(f"{endpoint}/api/json", params={"tree": "name,_class,url"})
        body = self._json_mapping(response)
        returned_name = _required_string(body.get("name"))
        if returned_name != name:
            raise JenkinsClientError("invalid_response")
        return JenkinsView(
            parent_path=parent_path,
            name=returned_name,
            jenkins_class=_optional_string(body.get("_class")),
            web_url=_optional_string(body.get("url")),
        )

    def _get_item_config(self, item_path: str) -> str:
        _, endpoint = _item_endpoint(item_path)
        response = self._get(f"{endpoint}/config.xml")
        return response.text

    def _get_view_config(self, parent_path: str | None, name: str) -> str:
        response = self._get(f"{_view_endpoint(parent_path, name)}/config.xml")
        return response.text

    def _trigger_build(
        self,
        item_path: str,
        parameters: Mapping[str, str],
        permit: WritePermit,
    ) -> int:
        """Submit one fixed Jenkins build request and return its queue identity, never retrying."""
        normalized_path, item_endpoint = _item_endpoint(item_path)
        if not isinstance(parameters, Mapping) or any(
            not isinstance(name, str) or not name or not isinstance(value, str)
            for name, value in parameters.items()
        ):
            raise JenkinsClientError("invalid build parameters")
        endpoint = f"{item_endpoint}/buildWithParameters" if parameters else f"{item_endpoint}/build"
        headers = self._run_write_headers()
        self._validate_run_permit(
            permit,
            action="trigger_build",
            item_path=normalized_path,
            parameters=dict(parameters),
            target_build_number=None,
        )
        self._record_sensitive_build_parameters(parameters)
        try:
            response = self._client.post(endpoint, data=dict(parameters), headers=headers)
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            if _write_response_has_unknown_outcome(response.status_code):
                raise JenkinsClientError("outcome_unknown")
            raise JenkinsClientError("http_status", status_code=response.status_code)
        queue_id = self._queue_id_from_location(response.headers.get("Location"))
        if queue_id is None:
            raise JenkinsClientError("outcome_unknown")
        return queue_id

    def get_queue_item(self, queue_id: int) -> JenkinsQueueItem:
        queue_id = _positive_int(queue_id)
        response = self._get(f"queue/item/{queue_id}/api/json")
        body = self._json_mapping(response)
        if _positive_int(body.get("id")) != queue_id:
            raise JenkinsClientError("invalid_response")
        cancelled = body.get("cancelled")
        if not isinstance(cancelled, bool):
            raise JenkinsClientError("invalid_response")
        executable = body.get("executable")
        if executable is None:
            executable_number = None
        elif isinstance(executable, Mapping):
            executable_number = _positive_int(executable.get("number"))
        else:
            raise JenkinsClientError("invalid_response")
        return JenkinsQueueItem(
            queue_id=queue_id,
            cancelled=cancelled,
            executable_number=executable_number,
            why=_optional_string(body.get("why")),
        )

    def get_build(self, item_path: str, number: int) -> JenkinsBuild:
        _, item_endpoint = _item_endpoint(item_path)
        number = _positive_int(number)
        response = self._get(f"{item_endpoint}/{number}/api/json")
        return _build_from_json(self._json_mapping(response), expected_number=number)

    def get_progressive_log(self, item_path: str, number: int, *, start: int) -> JenkinsProgressiveLog:
        _, item_endpoint = _item_endpoint(item_path)
        number = _positive_int(number)
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise JenkinsClientError("invalid log offset")
        endpoint = f"{item_endpoint}/{number}/logText/progressiveText"
        if not _is_fixed_progressive_log_endpoint(endpoint):
            raise JenkinsClientError("unsupported read endpoint")
        headers: dict[str, str] = {}
        if self._require_crumb:
            crumb_field, crumb_value = self._get_crumb()
            headers[crumb_field] = crumb_value
        try:
            response = self._client.get(endpoint, params={"start": str(start)}, headers=headers)
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise JenkinsClientError("http_status", status_code=response.status_code)
        size = _nonnegative_header_int(response.headers.get("X-Text-Size"))
        more_data = response.headers.get("X-More-Data")
        if size is None or size < start or more_data not in {None, "true", "false"}:
            raise JenkinsClientError("invalid_response")
        return JenkinsProgressiveLog(
            text=self._redact_log_text(response.text),
            next_start=size,
            more_data=more_data == "true",
        )

    def _cancel_build(self, item_path: str, number: int, permit: WritePermit) -> None:
        normalized_path, item_endpoint = _item_endpoint(item_path)
        number = _positive_int(number)
        endpoint = f"{item_endpoint}/{number}/stop"
        headers = self._run_write_headers()
        self._validate_run_permit(
            permit,
            action="cancel_build",
            item_path=normalized_path,
            parameters={},
            target_build_number=number,
        )
        try:
            response = self._client.post(endpoint, headers=headers)
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code == 302:
            if not self._is_same_controller_build_location(response.headers.get("Location"), item_endpoint, number):
                raise JenkinsClientError("outcome_unknown")
            return
        if response.status_code < 200 or response.status_code >= 300:
            if _write_response_has_unknown_outcome(response.status_code):
                raise JenkinsClientError("outcome_unknown")
            raise JenkinsClientError("http_status", status_code=response.status_code)

    def _get_junit_summary(self, item_path: str, number: int) -> JenkinsJUnitSummary:
        _, item_endpoint = _item_endpoint(item_path)
        number = _positive_int(number)
        body = self._json_mapping(self._get(f"{item_endpoint}/{number}/testReport/api/json"))
        try:
            return _junit_summary_from_body(body)
        except JenkinsClientError as exc:
            if exc.kind != "invalid_response":
                raise
            raise JenkinsClientError(
                "invalid_response", diagnostic=_junit_shape_diagnostic(body)
            ) from None

    def _get_pipeline_runs(self, item_path: str) -> tuple[JenkinsPipelineRun, ...]:
        _, item_endpoint = _item_endpoint(item_path)
        response = self._get(f"{item_endpoint}/wfapi/runs")
        body = self._json_list(response)
        return tuple(_pipeline_run_from_json(value) for value in body)

    def get_plugin_snapshot(self) -> tuple[JenkinsPlugin, ...]:
        response = self._get("pluginManager/api/json", params={"depth": "1"})
        body = self._json_mapping(response)
        plugins = body.get("plugins")
        if not isinstance(plugins, list):
            raise JenkinsClientError("invalid_response")
        result: list[JenkinsPlugin] = []
        for plugin in plugins:
            if not isinstance(plugin, Mapping):
                raise JenkinsClientError("invalid_response")
            short_name = _required_string(plugin.get("shortName"))
            active = plugin.get("active")
            if active is not None and not isinstance(active, bool):
                raise JenkinsClientError("invalid_response")
            result.append(
                JenkinsPlugin(
                    short_name=short_name,
                    version=_optional_string(plugin.get("version")),
                    active=active,
                )
            )
        return tuple(result)

    def _create_item(
        self,
        parent_path: str | None,
        name: str,
        item_type: str,
        template: str,
        permit: WritePermit,
        parameters: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(name, str) or not name or "/" in name or "\\" in name:
            raise JenkinsClientError("invalid item name")
        requested_parameters = dict(parameters or {})
        try:
            rendered = render_template(
                item_type=item_type,
                template=template,
                parameters=requested_parameters,
            )
        except TemplateError:
            raise JenkinsClientError("unsupported item template") from None
        if rendered.item_type == "view":
            raise JenkinsClientError("view templates must use the fixed createView operation")
        self._validate_write_permit(
            permit,
            item_path=_join_item_path(parent_path, name),
            item_type=item_type,
            template=template,
            parameters=requested_parameters,
        )
        if parent_path is None:
            endpoint = "createItem"
        else:
            _, parent_endpoint = _item_endpoint(parent_path)
            endpoint = f"{parent_endpoint}/createItem"
        headers = {"Content-Type": "application/xml"}
        if self._require_crumb:
            crumb_field, crumb_value = self._get_crumb()
            headers[crumb_field] = crumb_value
        try:
            response = self._client.post(
                endpoint,
                params={"name": name},
                content=rendered.xml.encode("utf-8"),
                headers=headers,
            )
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise JenkinsClientError("http_status", status_code=response.status_code)

    def _create_view(
        self,
        parent_path: str | None,
        name: str,
        template: str,
        permit: WritePermit,
    ) -> None:
        try:
            rendered = render_template(item_type="view", template=template, parameters={})
        except TemplateError:
            raise JenkinsClientError("unsupported view template") from None
        self._validate_write_permit(
            permit,
            item_path=_join_item_path(parent_path, name),
            item_type="view",
            template=template,
        )
        endpoint = _view_endpoint(parent_path, name, create=True)
        headers = {"Content-Type": "application/xml"}
        if self._require_crumb:
            crumb_field, crumb_value = self._get_crumb()
            headers[crumb_field] = crumb_value
        try:
            response = self._client.post(
                endpoint,
                params={"name": name},
                content=rendered.xml.encode("utf-8"),
                headers=headers,
            )
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise JenkinsClientError("http_status", status_code=response.status_code)

    def _update_item_config(self, item_path: str, xml: str, permit: WritePermit) -> None:
        normalized_path, endpoint = _item_endpoint(item_path)
        self._update_config(
            endpoint=f"{endpoint}/config.xml",
            item_path=normalized_path,
            xml=xml,
            permit=permit,
        )

    def _update_view_config(
        self,
        parent_path: str | None,
        name: str,
        xml: str,
        permit: WritePermit,
    ) -> None:
        self._update_config(
            endpoint=f"{_view_endpoint(parent_path, name)}/config.xml",
            item_path=_join_item_path(parent_path, name),
            xml=xml,
            permit=permit,
        )

    def _update_config(self, *, endpoint: str, item_path: str, xml: str, permit: WritePermit) -> None:
        if not isinstance(xml, str) or not xml:
            raise JenkinsClientError("invalid config payload")
        payload_digest = hashlib.sha256(xml.encode("utf-8")).hexdigest()
        operation = self._inspect_write_permit(
            permit,
            item_path=item_path,
            item_type=permit.item_type,
            template=permit.template,
            action="update_item",
            payload_digest=payload_digest,
        )
        with self._config_update_lock(item_path):
            self._update_config_locked(
                endpoint=endpoint,
                item_path=item_path,
                xml=xml,
                permit=permit,
                operation=operation,
            )

    def _update_config_locked(
        self,
        *,
        endpoint: str,
        item_path: str,
        xml: str,
        permit: WritePermit,
        operation: OperationRequest,
    ) -> None:
        try:
            before_xml = self._get(endpoint).text
            from .changes import ChangeValidationError, _config_digest, _parse_config, _validate_structured_update

            if permit.base_config_digest != _config_digest(_parse_config(before_xml)):
                raise JenkinsClientError("config conflict")
            _validate_structured_update(before_xml, xml, operation.fields)
        except ChangeValidationError:
            raise JenkinsClientError("invalid config payload") from None
        if self._write_authority is None or not self._write_authority.verify_and_consume_write_permit(permit):
            raise JenkinsClientError("write authorization is invalid")
        headers = {"Content-Type": "application/xml"}
        if self._require_crumb:
            crumb_field, crumb_value = self._get_crumb()
            headers[crumb_field] = crumb_value
        try:
            response = self._client.post(
                endpoint,
                content=xml.encode("utf-8"),
                headers=headers,
            )
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise JenkinsClientError("http_status", status_code=response.status_code)

    @contextmanager
    def _config_update_lock(self, item_path: str) -> Iterator[None]:
        """Serialize full-config writes for this controller in the current MCP process."""
        key = (*self._controller_update_lock_key, item_path)
        with _CONFIG_UPDATE_LOCKS_GUARD:
            entry = _CONFIG_UPDATE_LOCKS.get(key)
            if entry is None:
                entry = _ConfigUpdateLockEntry(lock=Lock())
                _CONFIG_UPDATE_LOCKS[key] = entry
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with _CONFIG_UPDATE_LOCKS_GUARD:
                entry.users -= 1
                if entry.users == 0 and _CONFIG_UPDATE_LOCKS.get(key) is entry:
                    del _CONFIG_UPDATE_LOCKS[key]

    def _get(self, path: str, *, params: Mapping[str, str] | None = None) -> httpx.Response:
        if not _is_fixed_read_endpoint(path):
            raise JenkinsClientError("unsupported read endpoint")
        headers: dict[str, str] = {}
        if self._require_crumb:
            crumb_field, crumb_value = self._get_crumb()
            headers[crumb_field] = crumb_value
        try:
            response = self._client.get(path, params=params, headers=headers)
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise JenkinsClientError("http_status", status_code=response.status_code)
        return response

    def _validate_write_permit(
        self,
        permit: WritePermit,
        *,
        item_path: str,
        item_type: str,
        template: str,
        action: str = "create_item",
        payload_digest: str | None = None,
        parameters: Mapping[str, str] | None = None,
    ) -> None:
        self._inspect_write_permit(
            permit,
            item_path=item_path,
            item_type=item_type,
            template=template,
            action=action,
            payload_digest=payload_digest,
            parameters=parameters,
        )
        if self._write_authority is None or not self._write_authority.verify_and_consume_write_permit(permit):
            raise JenkinsClientError("write authorization is invalid")

    def _validate_run_permit(
        self,
        permit: WritePermit,
        *,
        action: str,
        item_path: str,
        parameters: Mapping[str, str],
        target_build_number: int | None,
    ) -> None:
        operation = None if self._write_authority is None else self._write_authority.inspect_write_permit(permit)
        if (
            operation is None
            or permit.controller != self._controller_name
            or permit.action != action
            or permit.item_path != item_path
            or permit.item_type != "__jenkins_run__"
            or permit.template != "run-v1"
            or operation.action != action
            or operation.item_path != item_path
            or dict(operation.parameters) != dict(parameters)
            or operation.target_build_number != target_build_number
        ):
            raise JenkinsClientError("write authorization is invalid")
        if not self._write_authority.verify_and_consume_write_permit(permit):
            raise JenkinsClientError("write authorization is invalid")

    def _run_write_headers(self) -> dict[str, str]:
        if not self._require_crumb:
            return {}
        try:
            crumb_field, crumb_value = self._get_crumb()
        except JenkinsClientError as exc:
            raise JenkinsClientError(f"preflight_{exc.kind}", status_code=exc.status_code) from None
        return {crumb_field: crumb_value}

    def _record_sensitive_build_parameters(self, parameters: Mapping[str, str]) -> None:
        with self._log_redaction_lock:
            self._log_redaction_values.update(
                value for name, value in parameters.items() if _looks_sensitive_name(name) and value
            )

    def _redact_log_text(self, text: str) -> str:
        with self._log_redaction_lock:
            sensitive_values = tuple(sorted(self._log_redaction_values, key=len, reverse=True))
        redacted = _SENSITIVE_LOG_ASSIGNMENT.sub(r"\g<key>[REDACTED]", text)
        redacted = _SENSITIVE_LOG_HEADER.sub(r"\g<prefix>[REDACTED]", redacted)
        for value in sensitive_values:
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    def _inspect_write_permit(
        self,
        permit: WritePermit,
        *,
        item_path: str,
        item_type: str,
        template: str,
        action: str = "create_item",
        payload_digest: str | None = None,
        parameters: Mapping[str, str] | None = None,
    ) -> OperationRequest:
        operation = None if self._write_authority is None else self._write_authority.inspect_write_permit(permit)
        if (
            operation is None
            or permit.controller != self._controller_name
            or permit.action != action
            or permit.item_path != item_path
            or permit.item_type != item_type
            or permit.template != template
            or permit.payload_digest != payload_digest
            or (
                parameters is not None
                and dict(operation.parameters) != dict(parameters)
            )
            or (action == "update_item" and operation.change_digest != permit.payload_digest)
        ):
            raise JenkinsClientError("write authorization is invalid")
        return operation

    def _get_crumb(self) -> tuple[str, str]:
        if self._crumb is not None:
            return self._crumb
        try:
            response = self._client.get("crumbIssuer/api/json")
        except httpx.TimeoutException:
            raise JenkinsClientError("timeout") from None
        except httpx.HTTPError:
            raise JenkinsClientError("transport") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise JenkinsClientError("http_status", status_code=response.status_code)
        body = self._json_mapping(response)
        field = _required_string(body.get("crumbRequestField"))
        value = _required_string(body.get("crumb"))
        with self._log_redaction_lock:
            self._log_redaction_values.add(value)
        self._crumb = (field, value)
        return self._crumb

    def _queue_id_from_location(self, location: str | None) -> int | None:
        if not isinstance(location, str) or not location:
            return None
        parsed = urlsplit(location)
        if parsed.query or parsed.fragment:
            return None
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme != self._controller_url_parts.scheme
                or parsed.netloc != self._controller_url_parts.netloc
            ):
                return None
        elif location.startswith("//"):
            return None
        base_path = self._controller_url_parts.path.rstrip("/")
        prefix = f"{base_path}/queue/item/" if base_path else "/queue/item/"
        if not parsed.path.startswith(prefix):
            return None
        value = parsed.path[len(prefix) :].strip("/")
        if not value.isascii() or not value.isdecimal():
            return None
        queue_id = int(value)
        return queue_id if queue_id >= 1 else None

    def _is_same_controller_build_location(self, location: str | None, item_endpoint: str, number: int) -> bool:
        if not isinstance(location, str) or not location:
            return False
        parsed = urlsplit(location)
        if parsed.query or parsed.fragment:
            return False
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme != self._controller_url_parts.scheme
                or parsed.netloc != self._controller_url_parts.netloc
            ):
                return False
        elif location.startswith("//"):
            return False
        base_path = self._controller_url_parts.path.rstrip("/")
        expected_path = f"{base_path}/{item_endpoint}/{number}" if base_path else f"/{item_endpoint}/{number}"
        return parsed.path.rstrip("/") == expected_path

    @staticmethod
    def _json_mapping(response: httpx.Response) -> Mapping[str, Any]:
        try:
            body = response.json()
        except (ValueError, httpx.HTTPError):
            raise JenkinsClientError("invalid_response") from None
        if not isinstance(body, Mapping):
            raise JenkinsClientError("invalid_response")
        return body

    @staticmethod
    def _json_list(response: httpx.Response) -> list[Any]:
        try:
            body = response.json()
        except (ValueError, httpx.HTTPError):
            raise JenkinsClientError("invalid_response") from None
        if not isinstance(body, list):
            raise JenkinsClientError("invalid_response")
        return body


def _item_endpoint(item_path: str) -> tuple[str, str]:
    if not isinstance(item_path, str) or not item_path or item_path.startswith(("/", "\\")):
        raise JenkinsClientError("invalid item path")
    parts = item_path.split("/")
    if any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise JenkinsClientError("invalid item path")
    normalized_path = "/".join(parts)
    return normalized_path, "/".join(f"job/{quote(part, safe='')}" for part in parts)


def _view_endpoint(parent_path: str | None, name: str, *, create: bool = False) -> str:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        raise JenkinsClientError("invalid view name")
    if parent_path is None:
        return "createView" if create else f"view/{quote(name, safe='')}"
    _, parent_endpoint = _item_endpoint(parent_path)
    return f"{parent_endpoint}/createView" if create else f"{parent_endpoint}/view/{quote(name, safe='')}"


def _join_item_path(parent_path: str | None, name: str) -> str:
    if parent_path is None:
        return _item_endpoint(name)[0]
    normalized_parent, _ = _item_endpoint(parent_path)
    return _item_endpoint(f"{normalized_parent}/{name}")[0]


def _is_fixed_read_endpoint(path: str) -> bool:
    if path in {"api/json", "computer/api/json", "pluginManager/api/json"}:
        return True
    parts = path.split("/")
    if not parts or any(not part for part in parts) or path.startswith(("/", "\\")):
        return False
    if any(part in {".", ".."} or "\\" in part for part in parts):
        return False
    index = 0
    while index + 1 < len(parts) and parts[index] == "job":
        index += 2
    remainder = parts[index:]
    if parts[:2] == ["queue", "item"] and len(parts) == 5:
        return parts[2].isdecimal() and parts[3:] == ["api", "json"]
    if index > 0 and remainder in (["api", "json"], ["config.xml"]):
        return True
    if index > 0 and len(remainder) == 3 and remainder[0].isdecimal() and remainder[1:] == ["api", "json"]:
        return True
    if (
        index > 0
        and len(remainder) == 4
        and remainder[0].isdecimal()
        and remainder[1:] == ["testReport", "api", "json"]
    ):
        return True
    if index > 0 and remainder == ["wfapi", "runs"]:
        return True
    return (
        len(remainder) == 4
        and remainder[0] == "view"
        and remainder[2:] == ["api", "json"]
    ) or (len(remainder) == 3 and remainder[0] == "view" and remainder[2] == "config.xml")


def _is_fixed_progressive_log_endpoint(path: str) -> bool:
    parts = path.split("/")
    if not parts or any(not part for part in parts) or path.startswith(("/", "\\")):
        return False
    index = 0
    while index + 1 < len(parts) and parts[index] == "job":
        index += 2
    return (
        index > 0
        and len(parts[index:]) == 3
        and parts[index].isdecimal()
        and parts[index + 1 :] == ["logText", "progressiveText"]
    )


def _item_path_from_json(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise JenkinsClientError("invalid_response")
    full_name = _required_string(value.get("fullName"))
    return _item_endpoint(full_name)[0]


def _item_from_json(value: Any, *, item_path: str) -> JenkinsItem:
    if not isinstance(value, Mapping):
        raise JenkinsClientError("invalid_response")
    item = JenkinsItem(
        item_path=item_path,
        name=_required_string(value.get("name")),
        full_name=_required_string(value.get("fullName")),
        jenkins_class=_optional_string(value.get("_class")),
        web_url=_optional_string(value.get("url")),
        color=_optional_string(value.get("color")),
    )
    if item.full_name != item_path or item.name != item_path.rsplit("/", 1)[-1]:
        raise JenkinsClientError("invalid_response")
    return item


def _node_from_json(value: Any) -> JenkinsNode:
    if not isinstance(value, Mapping):
        raise JenkinsClientError("invalid_response")
    offline = value.get("offline")
    temporarily_offline = value.get("temporarilyOffline")
    idle = value.get("idle")
    if not all(isinstance(flag, bool) for flag in (offline, temporarily_offline, idle)):
        raise JenkinsClientError("invalid_response")
    return JenkinsNode(
        name=_required_string(value.get("displayName")),
        offline=offline,
        temporarily_offline=temporarily_offline,
        num_executors=_nonnegative_int(value.get("numExecutors")),
        idle=idle,
    )


def _required_string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise JenkinsClientError("invalid_response")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value)


def _positive_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise JenkinsClientError("invalid_response")
    return value


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise JenkinsClientError("invalid_response")
    return value


def _junit_summary_from_body(body: Mapping[str, Any]) -> JenkinsJUnitSummary:
    fail_count = _nonnegative_int(body.get("failCount"))
    skip_count = _nonnegative_int(body.get("skipCount"))
    if "totalCount" in body:
        total_count = _nonnegative_int(body["totalCount"])
    else:
        total_count = _junit_total_from_cases(body)
    if total_count < fail_count + skip_count:
        raise JenkinsClientError("invalid_response")
    return JenkinsJUnitSummary(
        total_count=total_count,
        fail_count=fail_count,
        skip_count=skip_count,
    )


def _junit_total_from_cases(body: Mapping[str, Any]) -> int:
    suites = body.get("suites")
    if not isinstance(suites, list):
        raise JenkinsClientError("invalid_response")
    total_count = 0
    for suite in suites:
        if not isinstance(suite, Mapping):
            raise JenkinsClientError("invalid_response")
        cases = suite.get("cases")
        if not isinstance(cases, list) or not all(isinstance(case, Mapping) for case in cases):
            raise JenkinsClientError("invalid_response")
        total_count += len(cases)
    return total_count


def _junit_shape_diagnostic(body: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    known_keys = set(_JUNIT_DIAGNOSTIC_KEYS)
    fields = [
        (name, type(body[name]).__name__ if name in body else "missing")
        for name in _JUNIT_DIAGNOSTIC_KEYS
    ]
    fields.append(("additional_field_count", str(len(set(body) - known_keys))))
    suites = body.get("suites")
    if not isinstance(suites, list):
        return tuple(fields)
    fields.append(("suite_count", str(len(suites))))
    fields.append(
        ("suite_item_types", "|".join(sorted({type(item).__name__ for item in suites})))
    )
    if not suites or not all(isinstance(item, Mapping) for item in suites):
        return tuple(fields)
    suite_mappings = [item for item in suites if isinstance(item, Mapping)]
    for name in _JUNIT_SUITE_DIAGNOSTIC_KEYS:
        fields.append((f"suite.{name}", _mapping_collection_type(suite_mappings, name)))
    suite_keys = set(_JUNIT_SUITE_DIAGNOSTIC_KEYS)
    fields.append(
        (
            "suite.additional_field_count",
            str(sum(len(set(item) - suite_keys) for item in suite_mappings)),
        )
    )
    case_lists = [item.get("cases") for item in suite_mappings]
    if not all(isinstance(cases, list) for cases in case_lists):
        return tuple(fields)
    case_items = [case for cases in case_lists for case in cases]
    fields.append(("case_count", str(len(case_items))))
    fields.append(
        ("case_item_types", "|".join(sorted({type(case).__name__ for case in case_items})))
    )
    return tuple(fields)


def _mapping_collection_type(items: list[Mapping[str, Any]], key: str) -> str:
    values = {type(item[key]).__name__ if key in item else "missing" for item in items}
    return "|".join(sorted(values))


def _nonnegative_header_int(value: str | None) -> int | None:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    return int(value)


def _write_response_has_unknown_outcome(status_code: int) -> bool:
    """A POST may have reached Jenkins even when its final response is unusable."""
    return 300 <= status_code < 400 or 500 <= status_code < 600


def _looks_sensitive_name(value: str) -> bool:
    normalized = value.casefold()
    return any(marker in normalized for marker in ("token", "password", "secret", "crumb", "cookie", "key"))


def _build_from_json(body: Mapping[str, Any], *, expected_number: int) -> JenkinsBuild:
    if _positive_int(body.get("number")) != expected_number:
        raise JenkinsClientError("invalid_response")
    building = body.get("building")
    if not isinstance(building, bool):
        raise JenkinsClientError("invalid_response")
    artifacts_raw = body.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise JenkinsClientError("invalid_response")
    artifacts: list[JenkinsArtifact] = []
    for value in artifacts_raw:
        if not isinstance(value, Mapping):
            raise JenkinsClientError("invalid_response")
        artifacts.append(
            JenkinsArtifact(
                file_name=_required_string(value.get("fileName")),
                display_path=_required_string(value.get("displayPath")),
                relative_path=_required_string(value.get("relativePath")),
            )
        )
    return JenkinsBuild(
        number=expected_number,
        building=building,
        result=_optional_string(body.get("result")),
        timestamp_ms=_nonnegative_int(body.get("timestamp")),
        duration_ms=_nonnegative_int(body.get("duration")),
        artifacts=tuple(artifacts),
    )


def _pipeline_run_from_json(value: Any) -> JenkinsPipelineRun:
    if not isinstance(value, Mapping):
        raise JenkinsClientError("invalid_response")
    end_time = value.get("endTimeMillis")
    if end_time is not None:
        end_time = _nonnegative_int(end_time)
    return JenkinsPipelineRun(
        run_id=_required_string(value.get("id")),
        name=_required_string(value.get("name")),
        status=_required_string(value.get("status")),
        start_time_ms=_nonnegative_int(value.get("startTimeMillis")),
        end_time_ms=end_time,
        duration_ms=_nonnegative_int(value.get("durationMillis")),
    )
