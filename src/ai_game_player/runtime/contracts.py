from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


RUNTIME_CONTRACT_VERSION = 1


class RuntimeCapability(str, Enum):
    CAPTURE = "capture"
    INPUT = "input"
    SAFETY = "safety"
    FAST_CV = "fast_cv"


@dataclass(frozen=True)
class RuntimeDescriptor:
    name: str
    implementation: str
    contract_version: int = RUNTIME_CONTRACT_VERSION
    capabilities: frozenset[RuntimeCapability] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("runtime name must not be empty")
        if not self.implementation.strip():
            raise ValueError("runtime implementation must not be empty")
        if self.contract_version <= 0:
            raise ValueError("contract_version must be positive")

    def supports(self, required: set[RuntimeCapability] | frozenset[RuntimeCapability]) -> bool:
        return set(required).issubset(self.capabilities)


@runtime_checkable
class RuntimeBackend(Protocol):
    @property
    def descriptor(self) -> RuntimeDescriptor:
        ...

    def is_healthy(self) -> bool:
        ...
