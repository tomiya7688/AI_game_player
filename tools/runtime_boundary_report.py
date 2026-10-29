"""同じ保存先の測定を試行IDで調停し、JSONを原子的に公開します。

測定開始時に旧成功をrunning状態で置換します。後から登録した試行だけが結果を
公開できます。既存の保存・OSファイルロックを使い、Coreの処理は変更しません。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
# 直接実行する開発ツールから、同じcheckoutの副作用のない保存機構を読みます。
from ai_game_player.atomic_json import stage_json_write  # noqa: E402
from ai_game_player.cross_process_lock import cross_process_file_lock  # noqa: E402


class BenchmarkReportAttempt:
    """出力先、試行ID、登録・完了状態を保持し、公開の所有者を確認します。"""

    def __init__(self, output: Path):
        self.output = output.resolve()
        self.run_id = uuid.uuid4().hex
        self.lock_path = self.output.with_name(f".{self.output.name}.lock")
        self._registered = False
        self._finished = False

    def _write(self, value: dict[str, object]) -> None:
        """同じディレクトリへ準備した完全なJSONだけを保存先へ置換します。"""
        staged = stage_json_write(self.output, value)
        try:
            staged.publish()
        finally:
            staged.discard()

    def _marker(self, status: str) -> dict[str, object]:
        return {
            "schema": "kadoka-runtime-boundary-attempt/v1",
            "run_id": self.run_id,
            "status": status,
        }

    def _owns_output(self) -> bool:
        """ロック保持中に、保存先がこの試行のIDをまだ持つか確認します。"""
        # 読込失敗は所有者の交代ではありません。失敗処理が所有権を再確認します。
        value = json.loads(self.output.read_text(encoding="utf-8"))
        return isinstance(value, dict) and value.get("run_id") == self.run_id

    def begin(self) -> None:
        """測定前に試行を登録し、過去の成功を現在の成功として読ませません。"""
        if self._registered:
            raise RuntimeError("benchmark attempt is already registered")
        with cross_process_file_lock(self.lock_path):
            self._write(self._marker("running"))
            self._registered = True

    def publish(self, report: dict[str, object]) -> None:
        """最後に登録した試行だけが、測定値をsuccess状態で公開します。"""
        if not self._registered or self._finished:
            raise RuntimeError("benchmark attempt is not active")
        with cross_process_file_lock(self.lock_path):
            if not self._owns_output():
                self._finished = True
                raise RuntimeError(
                    "a newer benchmark attempt owns the output; result discarded"
                )
            self._write({**report, "run_id": self.run_id, "status": "success"})
            self._finished = True

    def fail(self, error: Exception) -> bool:
        """この試行が所有する出力だけをfailed状態にし、新しい結果は保持します。"""
        if not self._registered or self._finished:
            return False
        with cross_process_file_lock(self.lock_path):
            if not self._owns_output():
                self._finished = True
                return False
            self._write(
                {
                    **self._marker("failed"),
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
            self._finished = True
            return True
