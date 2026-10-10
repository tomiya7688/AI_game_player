import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ai_game_player.event_journal import EventJournal
from ai_game_player.journal_adapters import LegacyEventAdapter

# {
#   責務: [RuntimeLog: application logを追記JSONLへ保存し、active SessionのeventをJournalにも記録する]
#   フィールド: [path: 旧JSONL互換readerの保存先, _event_adapter: active Session Journalへのoptional writer, _attachment_token: 現在のSession bindingを識別する所有token, _lock: attach・detach・writeを直列化するlock]
# }
class RuntimeLog:
    """Application runtime events written as JSONL."""

    # {
    #   責務: [__init__: application runtime logのJSONL保存先を設定する]
    #   処理: [path未指定時はuser_data/output/log/ai_game_player.jsonlを使用する]
    #   引数: [path: 旧runtime log JSONLの保存先]
    #   戻り値: []
    # }
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("user_data/output/log/ai_game_player.jsonl")
        self._event_adapter: LegacyEventAdapter | None = None
        self._attachment_token: object | None = None
        self._lock = threading.RLock()

    # {
    #   責務: [attach_event_journal: application logの旧JSONL recordをactive Session Journalへ移行する]
    #   処理: [現在のSessionに限ってevent adapterを作り、必要な初回だけ旧JSONLを移行して以後のwriteをJournalへ送る]
    #   引数: [journal: RuntimeLog eventの保存先となるactive Session Journal, migrate_legacy: falseなら過去JSONLの再取込を省く]
    #   戻り値: [object: このbindingだけを解除できる所有token]
    #   エラー: [OSError: 旧JSONLを読めない場合, ValueError: 旧JSONLに不正な行がある場合]
    #   エラー: [RuntimeError: 別Sessionが既にRuntimeLogを使用している場合]
    # }
    def attach_event_journal(
        self,
        journal: EventJournal,
        *,
        migrate_legacy: bool = True,
    ) -> object:
        with self._lock:
            if self._event_adapter is not None:
                raise RuntimeError("RuntimeLog is already attached to an active Session")
            event_adapter = LegacyEventAdapter(
                journal,
                self.path,
                "runtime.log",
                json_lines=True,
                retain_records=False,
                migrate_legacy=migrate_legacy,
            )
            attachment_token = object()
            self._event_adapter = event_adapter
            self._attachment_token = attachment_token
            return attachment_token

    # {
    #   責務: [detach_event_journal: Session終了後のlogをclosed Journalへ送らないよう解除する]
    #   処理: [一致する所有tokenを持つSessionだけがactive adapterを解除できる]
    #   引数: [attachment_token: attach_event_journalが返したbinding所有token]
    #   戻り値: []
    #   エラー: [RuntimeError: tokenが現在のSession bindingを所有しない場合]
    # }
    def detach_event_journal(self, attachment_token: object) -> None:
        with self._lock:
            if self._event_adapter is None:
                return
            if attachment_token is not self._attachment_token:
                raise RuntimeError("RuntimeLog attachment token does not own the active Session")
            self._event_adapter = None
            self._attachment_token = None

    # {
    #   責務: [write: runtime eventをJSONLへ追記し、active SessionがあればJournalへも記録する]
    #   処理: [timestamp・event名・message・contextを1 recordにし、旧JSONLをappendしてからJournalへ渡す]
    #   引数: [event: event種別, message: 利用者/開発者向け説明, context: eventに付随する構造化data]
    #   戻り値: []
    #   エラー: [OSError: JSONLまたはJournal保存先へ書けない場合]
    # }
    def write(self, event: str, message: str = "", context: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": str(event), "message": str(message), "context": dict(context or {})}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            if self._event_adapter is not None:
                self._event_adapter.prepare_append(record).publish()
