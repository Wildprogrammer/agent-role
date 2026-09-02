from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from .client import JenkinsClientError, JenkinsItem, JenkinsView
from .models import OperationRequest, WritePermit
from .service import OperationPolicyService
from .templates import TemplateError, render_template, template_identity


class ChangeConflictError(RuntimeError):
    pass


class ChangeValidationError(ValueError):
    pass


class _ChangeClient(Protocol):
    def get_item(self, item_path: str) -> JenkinsItem: ...

    def _get_item_config(self, item_path: str) -> str: ...

    def _update_item_config(self, item_path: str, xml: str, permit: WritePermit) -> None: ...

    def get_view(self, parent_path: str | None, name: str) -> JenkinsView: ...

    def _get_view_config(self, parent_path: str | None, name: str) -> str: ...

    def _update_view_config(
        self,
        parent_path: str | None,
        name: str,
        xml: str,
        permit: WritePermit,
    ) -> None: ...


@dataclass(frozen=True)
class ChangeRequest:
    controller: str
    item_path: str
    item_type: str
    template: str
    expected_config_digest: str
    fields: Mapping[str, object]
    expected_payload_digest: str | None = None
    template_parameters: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ConfigSnapshot:
    item_path: str
    item_type: str
    template: str
    digest: str
    supported_fields: tuple[str, ...]


@dataclass(frozen=True)
class ChangeResult:
    item_path: str
    changed_fields: tuple[str, ...]
    previous_digest: str
    current_digest: str


@dataclass(frozen=True)
class ChangePreview:
    item_path: str
    changed_fields: tuple[str, ...]
    previous_digest: str
    payload_digest: str
    current_digest: str


_SUPPORTED_FIELDS: Mapping[str, frozenset[str]] = {
    "folder": frozenset({"description"}),
    "view": frozenset({"description"}),
    "freestyle": frozenset({"description", "disabled", "cron", "parameters"}),
    "pipeline": frozenset(
        {"description", "disabled", "cron", "parameters", "pipeline_definition"}
    ),
    "multibranch": frozenset({"description", "disabled", "cron"}),
}
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")


class JenkinsChangeService:
    """Apply a small, typed edit to a Jenkins configuration with snapshot conflict checks."""

    def __init__(self, client: _ChangeClient, policy: OperationPolicyService) -> None:
        self._client = client
        self._policy = policy

    def snapshot(self, *, item_path: str, item_type: str, template: str) -> ConfigSnapshot:
        root = self._read_and_verify_item(item_path=item_path, item_type=item_type, template=template)
        return ConfigSnapshot(
            item_path=item_path,
            item_type=item_type,
            template=template,
            digest=_config_digest(root),
            supported_fields=tuple(sorted(_supported_fields(item_type))),
        )

    def preview(self, request: ChangeRequest) -> ChangePreview:
        normalized_fields, normalized_template_parameters = _normalize_change(request)
        if not _is_sha256_digest(request.expected_config_digest):
            raise ChangeValidationError("expected_config_digest must be a SHA-256 digest")
        eligibility = self._policy.check_eligibility(
            _update_operation(
                request,
                normalized_fields,
                normalized_template_parameters,
                change_digest=_preview_change_digest(normalized_fields),
            )
        )
        if not eligibility.allowed:
            raise ChangeValidationError(f"update_item denied: {eligibility.reason}")
        before_root = self._read_and_verify_item(
            item_path=request.item_path,
            item_type=request.item_type,
            template=request.template,
        )
        previous_digest = _config_digest(before_root)
        if previous_digest != request.expected_config_digest:
            raise ChangeConflictError("Jenkins configuration changed since the expected snapshot")
        updated_root = _copy_root(before_root)
        _apply_fields(
            updated_root,
            normalized_fields,
            item_type=request.item_type,
            template=request.template,
        )
        updated_xml = ET.tostring(updated_root, encoding="unicode")
        changed_fields = _changed_fields(before_root, updated_root, normalized_fields)
        return ChangePreview(
            item_path=request.item_path,
            changed_fields=changed_fields,
            previous_digest=previous_digest,
            payload_digest=hashlib.sha256(updated_xml.encode("utf-8")).hexdigest(),
            current_digest=_config_digest(updated_root),
        )

    def update(
        self,
        request: ChangeRequest,
        *,
        permit: WritePermit,
    ) -> ChangeResult:
        normalized_fields, operation = prepare_update_operation(request)
        if self._policy.inspect_write_permit(permit) != operation:
            raise ChangeValidationError("update_item denied: write authorization is invalid")

        before_root = self._read_and_verify_item(
            item_path=request.item_path,
            item_type=request.item_type,
            template=request.template,
        )
        previous_digest = _config_digest(before_root)
        if previous_digest != request.expected_config_digest:
            raise ChangeConflictError("Jenkins configuration changed since the expected snapshot")

        updated_root = _copy_root(before_root)
        _apply_fields(
            updated_root,
            normalized_fields,
            item_type=request.item_type,
            template=request.template,
        )
        updated_xml = ET.tostring(updated_root, encoding="unicode")
        expected_parameter_details = (
            _string_parameter_details(updated_root) if "parameters" in normalized_fields else None
        )
        payload_digest = hashlib.sha256(updated_xml.encode("utf-8")).hexdigest()
        if payload_digest != request.expected_payload_digest:
            raise ChangeConflictError("Jenkins configuration changed before the prepared update could be applied")
        changed_fields = _changed_fields(before_root, updated_root, normalized_fields)
        if not changed_fields:
            return ChangeResult(
                item_path=request.item_path,
                changed_fields=(),
                previous_digest=previous_digest,
                current_digest=previous_digest,
            )
        preserved_digest = _masked_config_digest(before_root, normalized_fields)
        try:
            self._write_config(request, updated_xml, permit)
            after_root = self._read_and_verify_item(
                item_path=request.item_path,
                item_type=request.item_type,
                template=request.template,
            )
        except JenkinsClientError:
            raise ChangeValidationError("Jenkins configuration update did not complete with readable evidence") from None

        _verify_applied_fields(
            after_root,
            normalized_fields,
            item_type=request.item_type,
            template=request.template,
            expected_parameter_details=expected_parameter_details,
        )
        if _masked_config_digest(after_root, normalized_fields) != preserved_digest:
            raise ChangeValidationError("Jenkins readback did not preserve unmodified configuration")
        return ChangeResult(
            item_path=request.item_path,
            changed_fields=changed_fields,
            previous_digest=previous_digest,
            current_digest=_config_digest(after_root),
        )

    def _read_and_verify_item(self, *, item_path: str, item_type: str, template: str) -> ET.Element:
        try:
            identity = template_identity(item_type=item_type, template=template)
        except TemplateError as exc:
            raise ChangeValidationError(str(exc)) from None
        try:
            if item_type == "view":
                parent_path, name = _split_view_path(item_path)
                view = self._client.get_view(parent_path, name)
                if (
                    view.parent_path != parent_path
                    or view.name != name
                    or view.jenkins_class != identity.expected_jenkins_class
                ):
                    raise ChangeValidationError("Jenkins view identity or type does not match the update template")
                root = _parse_config(self._client._get_view_config(parent_path, name))
            else:
                item = self._client.get_item(item_path)
                if (
                    item.item_path != item_path
                    or item.full_name != item_path
                    or item.name != item_path.rsplit("/", 1)[-1]
                    or item.jenkins_class != identity.expected_jenkins_class
                ):
                    raise ChangeValidationError("Jenkins item identity or type does not match the update template")
                root = _parse_config(self._client._get_item_config(item_path))
        except JenkinsClientError:
            raise ChangeValidationError("Jenkins configuration could not be read safely") from None
        if root.tag != identity.root_tag:
            raise ChangeValidationError("Jenkins configuration root does not match the update template")
        return root

    def _write_config(self, request: ChangeRequest, xml: str, permit: WritePermit) -> None:
        if request.item_type == "view":
            parent_path, name = _split_view_path(request.item_path)
            self._client._update_view_config(parent_path, name, xml, permit)
            return
        self._client._update_item_config(request.item_path, xml, permit)


def prepare_update_operation(
    request: ChangeRequest,
) -> tuple[dict[str, object], OperationRequest]:
    normalized_fields, normalized_template_parameters = _normalize_change(request)
    if not _is_sha256_digest(request.expected_config_digest) or not _is_sha256_digest(
        request.expected_payload_digest
    ):
        raise ChangeValidationError(
            "expected_config_digest and expected_payload_digest must be SHA-256 digests"
        )
    return normalized_fields, _update_operation(
        request,
        normalized_fields,
        normalized_template_parameters,
        change_digest=request.expected_payload_digest,
    )


def _supported_fields(item_type: str) -> frozenset[str]:
    fields = _SUPPORTED_FIELDS.get(item_type)
    if fields is None:
        raise ChangeValidationError(f"unsupported Jenkins item type {item_type!r}")
    return fields


def _split_view_path(item_path: str) -> tuple[str | None, str]:
    if not isinstance(item_path, str) or not item_path or item_path.startswith(("/", "\\")):
        raise ChangeValidationError("invalid Jenkins view path")
    parts = item_path.split("/")
    if any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise ChangeValidationError("invalid Jenkins view path")
    return "/".join(parts[:-1]) or None, parts[-1]


def _update_operation(
    request: ChangeRequest,
    fields: Mapping[str, object],
    template_parameters: Mapping[str, str] | None,
    *,
    change_digest: str,
) -> OperationRequest:
    return OperationRequest(
        controller=request.controller,
        action="update_item",
        item_path=request.item_path,
        item_type=request.item_type,
        template=request.template,
        fields=frozenset(fields),
        parameters={} if template_parameters is None else template_parameters,
        change_digest=change_digest,
        base_config_digest=request.expected_config_digest,
    )


def _preview_change_digest(fields: Mapping[str, object]) -> str:
    serialized = json.dumps(_json_safe_value(fields), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_safe_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    return value


def _normalize_change(
    request: ChangeRequest,
) -> tuple[dict[str, object], dict[str, str] | None]:
    has_template_update = request.template_parameters is not None
    fields = _validate_fields(
        request.item_type,
        request.fields,
        allow_empty=has_template_update,
    )
    if not has_template_update:
        return fields, None
    if request.item_type != "pipeline":
        raise ChangeValidationError("Pipeline template parameters require item_type='pipeline'")
    if not isinstance(request.template_parameters, Mapping):
        raise ChangeValidationError("template_parameters must be a mapping of strings")
    parameters = dict(request.template_parameters)
    if not all(isinstance(name, str) and isinstance(value, str) for name, value in parameters.items()):
        raise ChangeValidationError("template_parameters must be a mapping of strings")
    try:
        rendered = render_template(
            item_type=request.item_type,
            template=request.template,
            parameters=parameters,
        )
        _single_pipeline_definition(_parse_config(rendered.xml))
    except TemplateError as exc:
        raise ChangeValidationError(str(exc)) from None
    fields["pipeline_definition"] = parameters
    return fields, parameters


def _validate_fields(
    item_type: str,
    supplied: Mapping[str, object],
    *,
    allow_empty: bool = False,
) -> dict[str, object]:
    if not isinstance(supplied, Mapping) or (not supplied and not allow_empty):
        raise ChangeValidationError("update fields must be a nonempty mapping")
    fields = dict(supplied)
    if "pipeline_definition" in fields:
        raise ChangeValidationError(
            "pipeline_definition must be supplied through template_parameters"
        )
    unknown = set(fields) - (_supported_fields(item_type) - {"pipeline_definition"})
    if unknown:
        raise ChangeValidationError(f"unsupported update fields: {', '.join(sorted(unknown))}")
    for name, value in fields.items():
        if name == "description":
            _require_text(value, name, maximum=8192, allow_empty=True)
        elif name == "disabled":
            if not isinstance(value, bool):
                raise ChangeValidationError("disabled must be a boolean")
        elif name == "cron":
            if value is not None:
                _require_text(value, name, maximum=512, allow_empty=False)
        elif name == "parameters":
            if not isinstance(value, Mapping):
                raise ChangeValidationError("parameters must be a mapping of string defaults")
            for parameter_name, default in value.items():
                if not isinstance(parameter_name, str) or not _PARAMETER_NAME.fullmatch(parameter_name):
                    raise ChangeValidationError("parameter names must be simple Jenkins identifiers")
                _require_text(default, f"parameter {parameter_name!r}", maximum=4096, allow_empty=True)
    return fields


def _require_text(value: object, name: str, *, maximum: int, allow_empty: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ChangeValidationError(f"{name} must be a valid text value")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ChangeValidationError(f"{name} must not contain control characters")
    return value


def _parse_config(xml: str) -> ET.Element:
    if not isinstance(xml, str) or not xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ChangeValidationError("Jenkins configuration is not safe XML")
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        raise ChangeValidationError("Jenkins configuration is not valid XML") from None


def _config_digest(root: ET.Element) -> str:
    canonical = json.dumps(_canonical_element(root), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_element(element: ET.Element) -> object:
    return {
        "attributes": sorted(element.attrib.items()),
        "children": [_canonical_element(child) for child in element],
        "tag": element.tag,
        "text": (element.text or "").strip(),
    }


def _copy_root(root: ET.Element) -> ET.Element:
    return _parse_config(ET.tostring(root, encoding="unicode"))


def _apply_fields(
    root: ET.Element,
    fields: Mapping[str, object],
    *,
    item_type: str,
    template: str,
) -> None:
    if "description" in fields:
        _set_top_level_text(root, "description", fields["description"])
    if "disabled" in fields:
        _set_top_level_text(root, "disabled", "true" if fields["disabled"] else "false")
    if "cron" in fields:
        _set_cron(root, fields["cron"])
    if "parameters" in fields:
        _set_string_parameters(root, fields["parameters"])
    if "pipeline_definition" in fields:
        template_parameters = fields["pipeline_definition"]
        if not isinstance(template_parameters, Mapping):
            raise ChangeValidationError("pipeline_definition parameters are invalid")
        try:
            rendered = render_template(
                item_type=item_type,
                template=template,
                parameters=template_parameters,
            )
        except TemplateError as exc:
            raise ChangeValidationError(str(exc)) from None
        _replace_pipeline_definition(
            root,
            _single_pipeline_definition(_parse_config(rendered.xml)),
        )


def _changed_fields(
    before_root: ET.Element,
    after_root: ET.Element,
    requested_fields: Mapping[str, object],
) -> tuple[str, ...]:
    changed: list[str] = []
    for field in sorted(requested_fields):
        if field == "description":
            before, after = _text(before_root.find("description")), _text(after_root.find("description"))
        elif field == "disabled":
            before, after = _bool_text(before_root.find("disabled")), _bool_text(after_root.find("disabled"))
        elif field == "cron":
            before, after = _timer_spec(before_root), _timer_spec(after_root)
        elif field == "parameters":
            before, after = _string_parameters(before_root), _string_parameters(after_root)
        elif field == "pipeline_definition":
            before = _canonical_element(_single_pipeline_definition(before_root))
            after = _canonical_element(_single_pipeline_definition(after_root))
        else:
            raise ChangeValidationError(f"unsupported update field {field!r}")
        if before != after:
            changed.append(field)
    return tuple(changed)


def _masked_config_digest(root: ET.Element, fields: Collection[str]) -> str:
    masked = _copy_root(root)
    if "description" in fields:
        _remove_top_level(masked, "description")
    if "disabled" in fields:
        _remove_top_level(masked, "disabled")
    if "cron" in fields:
        triggers = masked.find("triggers")
        if triggers is not None:
            for trigger in list(triggers):
                if trigger.tag == "hudson.triggers.TimerTrigger":
                    triggers.remove(trigger)
    if "parameters" in fields:
        properties = masked.find("properties")
        if properties is not None:
            for property_element in list(properties):
                if property_element.tag == "hudson.model.ParametersDefinitionProperty":
                    properties.remove(property_element)
    if "pipeline_definition" in fields:
        masked.remove(_single_pipeline_definition(masked))
    return _config_digest(masked)


def _validate_structured_update(before_xml: str, after_xml: str, fields: Collection[str]) -> None:
    """Reject a config payload unless its only structural delta is policy-authorized."""
    before_root = _parse_config(before_xml)
    after_root = _parse_config(after_xml)
    if before_root.tag != after_root.tag:
        raise ChangeValidationError("Jenkins configuration root cannot be changed")
    if "description" in fields:
        _validate_leaf(after_root.find("description"), "description")
    if "disabled" in fields:
        disabled_element = after_root.find("disabled")
        _validate_leaf(disabled_element, "disabled")
        disabled = _text(disabled_element)
        if disabled not in {"true", "false"}:
            raise ChangeValidationError("disabled must remain a Jenkins boolean field")
    if "cron" in fields:
        _validate_timer_triggers(after_root)
    if "parameters" in fields:
        _validate_parameter_metadata_delta(before_root, after_root)
    if "pipeline_definition" in fields:
        _single_pipeline_definition(before_root)
        _single_pipeline_definition(after_root)
    if _masked_config_digest(before_root, fields) != _masked_config_digest(after_root, fields):
        raise ChangeValidationError("configuration contains changes outside the authorized structured fields")


def _validate_leaf(element: ET.Element | None, name: str) -> None:
    if element is not None and (element.attrib or list(element)):
        raise ChangeValidationError(f"{name} must remain a text-only Jenkins field")


def _validate_timer_triggers(root: ET.Element) -> None:
    triggers = root.find("triggers")
    if triggers is None:
        return
    for trigger in triggers:
        if trigger.tag != "hudson.triggers.TimerTrigger":
            continue
        if trigger.attrib or len(trigger) != 1 or trigger[0].tag != "spec":
            raise ChangeValidationError("cron must remain a standard Jenkins timer trigger")
        _validate_leaf(trigger[0], "cron spec")


def _remove_top_level(root: ET.Element, tag: str) -> None:
    element = root.find(tag)
    if element is not None:
        root.remove(element)


def _set_top_level_text(root: ET.Element, tag: str, value: object) -> None:
    element = root.find(tag)
    if element is None:
        element = ET.Element(tag)
        root.insert(1 if len(root) else 0, element)
    element.text = value if isinstance(value, str) else ""


def _set_cron(root: ET.Element, value: object) -> None:
    triggers = root.find("triggers")
    if triggers is None:
        if value is None:
            return
        triggers = ET.SubElement(root, "triggers")
    for trigger in list(triggers):
        if trigger.tag == "hudson.triggers.TimerTrigger":
            triggers.remove(trigger)
    if value is not None:
        trigger = ET.SubElement(triggers, "hudson.triggers.TimerTrigger")
        ET.SubElement(trigger, "spec").text = value


def _set_string_parameters(root: ET.Element, value: object) -> None:
    if not isinstance(value, Mapping):
        raise ChangeValidationError("parameters must be a mapping")
    properties = root.find("properties")
    if properties is None:
        properties = ET.SubElement(root, "properties")
    existing = [
        element
        for element in properties
        if element.tag == "hudson.model.ParametersDefinitionProperty"
    ]
    if existing:
        prior_details = {
            name: (description, trim)
            for name, _, description, trim in _string_parameter_definitions(root)
        }
    else:
        prior_details = {}
    for property_element in existing:
        properties.remove(property_element)
    if value:
        property_element = ET.SubElement(properties, "hudson.model.ParametersDefinitionProperty")
        definitions = ET.SubElement(property_element, "parameterDefinitions")
        for name in sorted(value):
            definition = ET.SubElement(definitions, "hudson.model.StringParameterDefinition")
            ET.SubElement(definition, "name").text = name
            description, trim = prior_details.get(name, ("", False))
            ET.SubElement(definition, "description").text = description
            ET.SubElement(definition, "defaultValue").text = value[name]
            ET.SubElement(definition, "trim").text = "true" if trim else "false"


def _single_pipeline_definition(root: ET.Element) -> ET.Element:
    definitions = [child for child in root if child.tag == "definition"]
    if len(definitions) != 1:
        raise ChangeValidationError(
            "Pipeline config must contain exactly one top-level definition"
        )
    return definitions[0]


def _replace_pipeline_definition(root: ET.Element, desired: ET.Element) -> None:
    current = _single_pipeline_definition(root)
    index = list(root).index(current)
    root.remove(current)
    root.insert(index, _copy_root(desired))


def _verify_applied_fields(
    root: ET.Element,
    fields: Mapping[str, object],
    *,
    item_type: str,
    template: str,
    expected_parameter_details: Mapping[str, tuple[str, str, bool]] | None = None,
) -> None:
    if "description" in fields and _text(root.find("description")) != fields["description"]:
        raise ChangeValidationError("Jenkins readback did not retain the requested description")
    if "disabled" in fields and _bool_text(root.find("disabled")) != fields["disabled"]:
        raise ChangeValidationError("Jenkins readback did not retain the requested disabled state")
    if "cron" in fields and _timer_spec(root) != fields["cron"]:
        raise ChangeValidationError("Jenkins readback did not retain the requested cron trigger")
    if "parameters" in fields:
        if expected_parameter_details is None or _string_parameter_details(root) != expected_parameter_details:
            raise ChangeValidationError("Jenkins readback did not retain the requested string parameter details")
    if "pipeline_definition" in fields:
        template_parameters = fields["pipeline_definition"]
        if not isinstance(template_parameters, Mapping):
            raise ChangeValidationError("pipeline_definition parameters are invalid")
        try:
            expected_root = _parse_config(
                render_template(
                    item_type=item_type,
                    template=template,
                    parameters=template_parameters,
                ).xml
            )
        except TemplateError as exc:
            raise ChangeValidationError(str(exc)) from None
        if _canonical_element(_single_pipeline_definition(root)) != _canonical_element(
            _single_pipeline_definition(expected_root)
        ):
            raise ChangeValidationError(
                "Jenkins readback did not retain the requested Pipeline definition"
            )


def _timer_spec(root: ET.Element) -> str | None:
    triggers = root.find("triggers")
    if triggers is None:
        return None
    matching = [trigger for trigger in triggers if trigger.tag == "hudson.triggers.TimerTrigger"]
    if not matching:
        return None
    if len(matching) != 1:
        raise ChangeValidationError("Jenkins configuration has multiple timer triggers")
    return _text(matching[0].find("spec"))


def _string_parameters(root: ET.Element) -> dict[str, str]:
    return {name: default for name, default, _, _ in _string_parameter_definitions(root)}


def _string_parameter_details(root: ET.Element) -> dict[str, tuple[str, str, bool]]:
    return {
        name: (default, description, trim)
        for name, default, description, trim in _string_parameter_definitions(root)
    }


def _validate_parameter_metadata_delta(before_root: ET.Element, after_root: ET.Element) -> None:
    before = _string_parameter_details(before_root)
    after = _string_parameter_details(after_root)
    for name in before.keys() & after.keys():
        _, before_description, before_trim = before[name]
        _, after_description, after_trim = after[name]
        if (after_description, after_trim) != (before_description, before_trim):
            raise ChangeValidationError("parameter description and trim cannot change through a default-value update")
    for name in after.keys() - before.keys():
        _, description, trim = after[name]
        if description or trim:
            raise ChangeValidationError("new string parameters must use empty descriptions and trim=false")


def _string_parameter_definitions(root: ET.Element) -> tuple[tuple[str, str, str, bool], ...]:
    properties = root.find("properties")
    if properties is None:
        return ()
    values: list[tuple[str, str, str, bool]] = []
    names: set[str] = set()
    for property_element in properties:
        if property_element.tag != "hudson.model.ParametersDefinitionProperty":
            continue
        if property_element.attrib or len(property_element) != 1 or property_element[0].tag != "parameterDefinitions":
            raise ChangeValidationError("Jenkins parameter property is not safely structured")
        definitions = property_element[0]
        if definitions.attrib:
            raise ChangeValidationError("Jenkins parameter definitions are not safely structured")
        for definition in definitions:
            if definition.tag != "hudson.model.StringParameterDefinition":
                raise ChangeValidationError("unknown Jenkins parameter definition cannot be overwritten")
            tags = [child.tag for child in definition]
            if definition.attrib or tags not in (
                ["name", "description", "defaultValue"],
                ["name", "description", "defaultValue", "trim"],
            ) or any(child.attrib or list(child) for child in definition):
                raise ChangeValidationError("Jenkins string parameter definition is not safely structured")
            name = _text(definition.find("name"))
            description = _text(definition.find("description"))
            default = _text(definition.find("defaultValue"))
            trim_text = _text(definition.find("trim"))
            if trim_text not in {None, "true", "false"}:
                raise ChangeValidationError("Jenkins string parameter trim must be a boolean")
            if name is None or description is None or default is None or name in names:
                raise ChangeValidationError("Jenkins string parameter definition is invalid")
            names.add(name)
            values.append((name, default, description, trim_text == "true"))
    return tuple(values)


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    return element.text or ""


def _bool_text(element: ET.Element | None) -> bool | None:
    text = _text(element)
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _is_sha256_digest(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
