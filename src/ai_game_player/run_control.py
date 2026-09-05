class RunController:
    """Pipeline execution state shared by the GUI and orchestration layer."""

    def __init__(self) -> None:
        self._running = True

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def ensure_running(self) -> None:
        if not self._running:
            raise RuntimeError("実行は停止されています")