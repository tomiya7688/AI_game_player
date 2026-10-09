from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from ai_game_player.event_journal import EventEnvelope, EventJournal


# {
#   責務: [StagedJournalAppend: 旧JSON storeの公開時点を保ちながらSession Journal appendを遅延する]
#   フィールド: [_publish: 実行世代の確認後にJournalへ保存するcallback]
# }
class StagedJournalAppend:
    # {
    #   責務: [__init__: publish時に実行するappend callbackを保管する]
    #   処理: [callbackをpublish呼出しまで保持する]
    #   引数: [publish: EventJournalへrecordを保存し互換cacheを更新する関数]
    #   戻り値: []
    # }
    def __init__(self, publish: Callable[[], EventEnvelope]) -> None:
        self._publish = publish
        self._published = False

    # {
    #   責務: [publish: 準備したlegacy eventをSession Event Journalへ一度だけ保存する]
    #   処理: [呼出し時にSQLite appendを実行し、commit完了を確認する]
    #   引数: [なし]
    #   戻り値: [EventEnvelope: Journalへcommitしたevent]
    #   エラー: [EventJournalError: eventがJournal契約に合わない場合、またはDB commitに失敗した場合]
    # }
    def publish(self) -> EventEnvelope:
        if self._published:
            raise RuntimeError("staged journal append was already published")
        event = self._publish()
        self._published = True
        return event

    # {
    #   責務: [discard: publish前のappend callbackを破棄する]
    #   処理: [SQLite書込みをpublishまで遅延するため追加resourceを解放しない]
    #   引数: [なし]
    #   戻り値: []
    # }
    def discard(self) -> None:
        self._published = True


# {
#   責務: [LegacyEventAdapter: 旧JSON/JSONL記録をSession Journalへ一度取り込み、新規recordを追記する]
#   フィールド: [journal: 現Sessionの追記先, source_path: 旧形式recordの読込元, event_type: envelopeへ付けるrecord種別, _records: 旧形式readerへ返す互換cache]
# }
class LegacyEventAdapter:
    # {
    #   責務: [__init__: 旧保存ファイルを検証し、既存recordを重複しないIDでJournalへ移行する]
    #   処理: [JSON arrayまたはJSONLを読み、各recordの内容hashから安定event IDを作って移行再試行を冪等にする]
    #   引数: [journal: event保存先, source_path: 旧形式JSONまたはJSONL, event_type: 移行先Envelopeの種類, json_lines: trueなら各行を1 objectとして読む, retain_records: falseなら読み込み後のlegacy recordを互換cacheに保持しない]
    #   戻り値: []
    #   エラー: [OSError: 旧ファイルを読めない場合, ValueError: JSON形状またはrecordが不正な場合]
    # }
    def __init__(
        self,
        journal: EventJournal,
        source_path: Path,
        event_type: str,
        *,
        json_lines: bool = False,
        retain_records: bool = True,
    ) -> None:
        self.journal = journal
        self.source_path = Path(source_path)
        self.event_type = event_type
        self.json_lines = json_lines
        self.retain_records = retain_records
        self._records: list[dict[str, Any]] = []
        for index, record in enumerate(self._read_legacy_records()):
            if self.retain_records:
                self._records.append(record)
            self._append_record(record, self._legacy_event_id(index, record))

    # {
    #   責務: [records: 旧readerと同じrecord形状で旧・現Sessionの記録を返す]
    #   処理: [adapterが読み込んだlegacy recordとSession内で追加したrecordのcopyを返す]
    #   引数: [self: 互換recordを保持するadapter]
    #   戻り値: [list[dict[str, Any]]: 旧形式を保ったevent record列]
    # }
    @property
    def records(self) -> list[dict[str, Any]]:
        return deepcopy(self._records)

    # {
    #   責務: [prepare_append: legacy API用recordをcommit時まで保留したJournal appendへ変換する]
    #   処理: [publishまでrecord cacheを更新せず、実行世代を確認した呼出側でcommitする]
    #   引数: [record: 旧storeが公開してきた辞書形式のevent]
    #   戻り値: [StagedJournalAppend: commitまたは破棄を選べる遅延append]
    #   エラー: [ValueError: recordがJSON objectではない場合]
    # }
    def prepare_append(self, record: Mapping[str, Any]) -> StagedJournalAppend:
        if not isinstance(record, Mapping):
            raise ValueError("legacy event record must be an object")
        copied_record = deepcopy(dict(record))

        def publish_record() -> EventEnvelope:
            event = self._append_record(copied_record)
            if self.retain_records:
                self._records.append(copied_record)
            return event

        return StagedJournalAppend(publish_record)

    # {
    #   責務: [_append_record: legacy recordを指定event typeのJournal Envelopeとしてcommitする]
    #   処理: [recordのsnapshot/frame/assessment IDをEnvelopeの関連fieldへ移し、互換record全体をpayloadに保持する]
    #   引数: [record: Journalへ保存する旧形式event, event_id: migration再試行で同一recordを識別する任意ID]
    #   戻り値: [EventEnvelope: sequenceとSession IDを持つcommit済みevent]
    # }
    def _append_record(
        self,
        record: Mapping[str, Any],
        event_id: str | None = None,
    ) -> EventEnvelope:
        return self.journal.append(
            self.event_type,
            event_id=event_id,
            status=_optional_text(record.get("status")),
            frame_id=_optional_text(record.get("frame_id")),
            snapshot_id=_optional_text(record.get("snapshot_id")),
            correlation_id=_optional_text(
                record.get("assessment_id", record.get("correlation_id"))
            ),
            payload={"legacy_record": dict(record)},
        )

    # {
    #   責務: [_read_legacy_records: 指定された旧ファイルを非破壊で読み込む]
    #   処理: [JSONLなら各行、JSON arrayなら各要素を読み、object以外のrecordを拒否する]
    #   引数: [self: legacy file pathとformatを持つadapter]
    #   戻り値: [list[dict[str, Any]]: migration順に並ぶ旧形式record]
    #   エラー: [OSError: ファイル読込に失敗した場合, ValueError: 配列・object・JSONL行が不正な場合]
    # }
    def _read_legacy_records(self) -> list[dict[str, Any]]:
        if not self.source_path.exists():
            return []
        text = self.source_path.read_text(encoding="utf-8")
        if self.json_lines:
            values = [
                json.loads(line)
                for line in text.splitlines()
                if line.strip()
            ]
        else:
            values = json.loads(text)
            if not isinstance(values, list):
                raise ValueError(f"{self.source_path.name} must contain an array")
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError(f"{self.source_path.name} records must be JSON objects")
        return values

    # {
    #   責務: [_legacy_event_id: 旧record内容からmigration retry用の安定event IDを生成する]
    #   処理: [event type・行番号・厳密JSON表現をUTF-8化してSHA-256 hashへ変換する]
    #   引数: [index: 旧ファイル内の0-based位置, record: hash対象の旧形式event]
    #   戻り値: [str: 同じfile positionと内容で再試行した場合に一致するlegacy ID]
    # }
    def _legacy_event_id(self, index: int, record: Mapping[str, Any]) -> str:
        canonical_record = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_decimal_json_default,
        )
        identity = f"{self.event_type}\0{self.source_path.name}\0{index}\0{canonical_record}"
        return f"legacy-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


# {
#   責務: [_optional_text: legacy recordの任意関連fieldをEnvelope用の文字列へ正規化する]
#   処理: [非空stringだけを返し、欠落・空文字・別型は関連付けなしとして扱う]
#   引数: [value: legacy recordの関連field候補]
#   戻り値: [str | None: Envelopeへ設定する値]
# }
def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# {
#   責務: [_decimal_json_default: JSON hash用にDecimal値を安定した文字列表現へ変換する]
#   処理: [Decimalだけを文字列化し、JSONで扱えない別型は拒否する]
#   引数: [value: JSON encoderが標準対応していないmigration record値]
#   戻り値: [str: Decimalの桁を保つ文字列表現]
#   エラー: [TypeError: Decimal以外の未対応型を受け取った場合]
# }
def _decimal_json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize legacy value of type {type(value).__name__}")
