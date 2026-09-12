from collections.abc import Iterable

from ai_game_player.runtime.contracts import RuntimeBackend, RuntimeCapability


class RuntimeRegistry:
    """Registers interchangeable runtime backends without exposing implementation internals."""

    def __init__(self) -> None:
        self._backends: dict[str, RuntimeBackend] = {}

    def register(self, backend: RuntimeBackend) -> None:
        name = backend.descriptor.name
        if name in self._backends:
            raise ValueError(f"runtime backend already registered: {name}")
        self._backends[name] = backend

    def get(self, name: str) -> RuntimeBackend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise KeyError(f"runtime backend not found: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._backends)

    def resolve(
        self,
        required: Iterable[RuntimeCapability],
        *,
        healthy_only: bool = True,
    ) -> RuntimeBackend:
        required_set = set(required)
        for backend in self._backends.values():
            if healthy_only and not backend.is_healthy():
                continue
            if backend.descriptor.supports(required_set):
                return backend
        requested = ", ".join(sorted(capability.value for capability in required_set)) or "none"
        raise LookupError(f"no runtime backend satisfies capabilities: {requested}")
