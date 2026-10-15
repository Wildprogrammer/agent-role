from .catalog import component_descriptor_path, resolve_inside
from .contracts import (
    CompositionContractError,
    parse_component_reference,
    validate_parameter_mapping,
)
from .engine import (
    PLACEHOLDER,
    CompositionAdapter,
    ExpandedComponent,
    compose_scenario,
    inspect_placeholders,
    rewrite_placeholders,
)
from .lifecycle import (
    LifecycleEvent,
    LifecyclePhase,
    LifecycleState,
    initial_state,
    transition,
)
from .models import (
    EFFECT_RANK,
    ComponentDefinition,
    ComponentProvenance,
    ComponentUse,
    Effect,
    ParameterDeclaration,
    ScenarioDefinition,
)

__all__ = [
    "EFFECT_RANK",
    "ComponentDefinition",
    "ComponentProvenance",
    "ComponentUse",
    "CompositionContractError",
    "CompositionAdapter",
    "Effect",
    "ExpandedComponent",
    "LifecycleEvent",
    "LifecyclePhase",
    "LifecycleState",
    "ParameterDeclaration",
    "PLACEHOLDER",
    "ScenarioDefinition",
    "component_descriptor_path",
    "compose_scenario",
    "initial_state",
    "inspect_placeholders",
    "parse_component_reference",
    "resolve_inside",
    "rewrite_placeholders",
    "transition",
    "validate_parameter_mapping",
]
