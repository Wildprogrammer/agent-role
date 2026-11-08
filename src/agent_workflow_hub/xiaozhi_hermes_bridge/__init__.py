"""Safe, transport-neutral bridge between Xiaozhi and persistent agent hosts."""

from .contracts import (
    BridgeConfig,
    BridgeContractError,
    BridgeRequest,
    DshWebConfig,
    ProfileRoute,
    read_config,
    read_request,
)
from .dsh_web_gateway import DeepSeekHarnessWebGateway, DshGatewayError
from .service import BridgeService, HermesRunResult, HermesRunsTransport
from .binding_store import BindingStore, BindingStoreError, SessionBinding
from .desktop_gateway import (
    CompressionResult,
    CreatedSessionResult,
    DesktopGatewayError,
    HermesDesktopGateway,
    SharedTurnResult,
    default_discovery_paths,
    discover_desktop_backend,
    discover_desktop_gateway_backend,
)
from .shared_service import SharedConversationService
from .control_events import ControlEvent, ControlEventStore, ControlEventStoreError
from .control_http import ControlHttpServers, ScopedAuth
from .control_plane import ControlPlaneError, DeviceControlCoordinator
from .control_runtime import LingxiControlRuntime, control_runtime_from_environment
from .inprocess_control import InProcessDeviceControlBackend
from .runtime_settings import RuntimeSettingsError, RuntimeSettingsManager
from .xiaozhi_server_adapter import (
    BridgeProcessClient,
    BridgeProcessSettings,
    XiaozhiAdapterError,
    XiaozhiHermesAdapter,
    adapter_from_environment,
)

__all__ = [
    "BridgeConfig",
    "BridgeContractError",
    "BridgeRequest",
    "BridgeService",
    "BindingStore",
    "BindingStoreError",
    "CompressionResult",
    "ControlEvent",
    "ControlEventStore",
    "ControlEventStoreError",
    "ControlHttpServers",
    "ControlPlaneError",
    "CreatedSessionResult",
    "DesktopGatewayError",
    "DeviceControlCoordinator",
    "DeepSeekHarnessWebGateway",
    "DshGatewayError",
    "DshWebConfig",
    "HermesDesktopGateway",
    "InProcessDeviceControlBackend",
    "LingxiControlRuntime",
    "BridgeProcessClient",
    "BridgeProcessSettings",
    "HermesRunResult",
    "HermesRunsTransport",
    "SessionBinding",
    "ScopedAuth",
    "SharedConversationService",
    "SharedTurnResult",
    "default_discovery_paths",
    "discover_desktop_backend",
    "discover_desktop_gateway_backend",
    "ProfileRoute",
    "XiaozhiAdapterError",
    "XiaozhiHermesAdapter",
    "adapter_from_environment",
    "control_runtime_from_environment",
    "read_config",
    "read_request",
    "RuntimeSettingsError",
    "RuntimeSettingsManager",
]
