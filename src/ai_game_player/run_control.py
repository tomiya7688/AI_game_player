class RunController:
    """Pipeline execution state shared by the GUI and orchestration layer."""

    def __init__(self) -> None:
        self._running = True
        self._rearm_token = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rearm_token(self) -> int:
        """Monotonic token incremented only by an explicit start/re-arm action."""
        return self._rearm_token

    def start(self) -> None:
        from ai_game_player.safety_guard import rearm_default_emergency_stop

        rearm_default_emergency_stop()
        self._rearm_token += 1
        self._running = True

    def stop(self) -> None:
        self._running = False

    def ensure_running(self) -> None:
        if not self._running:
            raise RuntimeError("実行は停止されています")