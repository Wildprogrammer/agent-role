"""Windows desktop-client automation trace and Airtest bundle support."""

from .contracts import ContractError, load_request
from .renderer import compile_bundle

__all__ = ["ContractError", "compile_bundle", "load_request"]
