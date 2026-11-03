from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from ai_game_player.stage_envelope import (
    STAGE_ENVELOPE_MAX_DEPTH,
    STAGE_ENVELOPE_MAX_ENCODED_BYTES,
    STAGE_ENVELOPE_SCHEMA_VERSION,
    StageEnvelope,
    StageEnvelopeError,
)


# UTF-8 BOM（U+FEFF）。不可視文字をliteralで埋め込まず、escapeで定義する。
_BYTE_ORDER_MARK = "\ufeff"
# 版Nの文書を版N+1の文書へ変換する移行関数の型。入力文書を変更せず、新しいobjectを返す。
StageEnvelopeMigration = Callable[[Mapping[str, Any]], Mapping[str, Any]]
# 既定の移行表。v1が最初の公開版なので空であり、v1以外の版はすべてunsupported_versionで拒否する。
STAGE_ENVELOPE_MIGRATIONS: Mapping[int, StageEnvelopeMigration] = {}


# {
#   責務: [encode_stage_envelope: 検証済みEnvelopeを、言語をまたいで同じ値として読めるcanonical JSONのUTF-8 bytesへ変換する]
#   処理: [
#     1: to_dictで全fieldを含む素のJSON値へ変換する
#     2: key昇順・空白なし・非ASCII文字はそのまま・NaN/Infinity禁止でJSON化しUTF-8へencodeする
#     3: byte数が上限を超えたら送出せず拒否する
#   ]
#   引数: [envelope: 送出する検証済みStageEnvelope]
#   戻り値: [bytes: 同じEnvelopeから常に同じ並びになるUTF-8 JSON（BOMなし）]
#   エラー: [StageEnvelopeError(limit_exceeded): encode結果がSTAGE_ENVELOPE_MAX_ENCODED_BYTESを超える場合]
# }
def encode_stage_envelope(envelope: StageEnvelope) -> bytes:
    text = json.dumps(
        envelope.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    encoded = text.encode("utf-8")
    if len(encoded) > STAGE_ENVELOPE_MAX_ENCODED_BYTES:
        raise StageEnvelopeError(
            StageEnvelopeError.LIMIT_EXCEEDED,
            "encoded stage envelope exceeds the byte limit",
        )
    return encoded


# {
#   責務: [decode_stage_envelope: 外部から届いたJSON文書を、parse前の上限確認と厳密なJSON規則を通して検証済みEnvelopeへ復元する]
#   処理: [
#     1: UTF-8 byte数を上限と比較し、BOM・不正UTF-8を拒否する
#     2: 文字列外のobject・配列の入れ子深さをparse前に数え、上限超過を再帰parseの前に拒否する
#     3: 重複key・NaN/Infinity・非有限数を拒否してparseし、root objectを移行点へ渡す
#     4: 現行版へ移行した文書をStageEnvelope.from_dictで厳密に検証する
#   ]
#   引数: [data: UTF-8 bytesまたは文字列のJSON文書, migrations: 版ごとの移行関数表。省略時はSTAGE_ENVELOPE_MIGRATIONS]
#   戻り値: [StageEnvelope: v1契約に適合するEnvelope]
#   エラー: [StageEnvelopeError: 上限超過はlimit_exceeded、未知版・移行不能はunsupported_version、それ以外はmalformed]
# }
def decode_stage_envelope(
    data: bytes | str,
    migrations: Mapping[int, StageEnvelopeMigration] | None = None,
) -> StageEnvelope:
    text = _decode_utf8_text(data)
    _reject_excessive_nesting(text)
    try:
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except StageEnvelopeError:
        raise
    except ValueError as error:
        # JSONDecodeErrorに加え、桁数上限を超える整数literalのValueErrorもmalformedとして扱う。
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, f"invalid JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "stage envelope must be a JSON object"
        )
    current = migrate_stage_envelope_document(
        document, STAGE_ENVELOPE_MIGRATIONS if migrations is None else migrations
    )
    return StageEnvelope.from_dict(current)


# {
#   責務: [migrate_stage_envelope_document: 古い版の文書を、登録済み移行関数の連鎖で現行版の文書へ進める唯一の移行点]
#   処理: [
#     1: schema_versionがboolでない整数か確認する
#     2: 現行版より新しい版は未知として拒否し、古い版は版N→N+1の関数を順に適用する
#     3: 途中の版に移行関数がない、または関数が版を1つ進めなかった場合は移行不能として拒否する
#   ]
#   引数: [document: parse済みのEnvelope object, migrations: 版Nを受け取りN+1の文書を返す関数の表]
#   戻り値: [Mapping[str, Any]: schema_versionが現行版の文書。現行版の入力はそのまま返す]
#   エラー: [StageEnvelopeError: 版が整数でなければmalformed、未知・移行不能ならunsupported_version]
#   補足: [移行後の文書は呼び出し元がStageEnvelope.from_dictで改めて厳密検証する]
# }
def migrate_stage_envelope_document(
    document: Mapping[str, Any],
    migrations: Mapping[int, StageEnvelopeMigration],
) -> Mapping[str, Any]:
    version = _document_version(document)
    if version > STAGE_ENVELOPE_SCHEMA_VERSION:
        raise StageEnvelopeError(
            StageEnvelopeError.UNSUPPORTED_VERSION,
            f"stage envelope schema_version {version} is newer than {STAGE_ENVELOPE_SCHEMA_VERSION}",
        )
    current = document
    while version < STAGE_ENVELOPE_SCHEMA_VERSION:
        migration = migrations.get(version)
        if migration is None:
            raise StageEnvelopeError(
                StageEnvelopeError.UNSUPPORTED_VERSION,
                f"no migration is registered for stage envelope schema_version {version}",
            )
        current = migration(current)
        next_version = _document_version(current)
        if next_version != version + 1:
            raise StageEnvelopeError(
                StageEnvelopeError.UNSUPPORTED_VERSION,
                f"migration from schema_version {version} produced {next_version}",
            )
        version = next_version
    return current


# {
#   責務: [_document_version: 移行前後の文書からschema_versionを整数として取り出す]
#   処理: [objectであり、schema_versionがboolでない整数であることを確認する]
#   引数: [document: 版を読むEnvelope object]
#   戻り値: [int: 文書の版]
#   エラー: [StageEnvelopeError(malformed): objectでない、または版が整数でない場合]
# }
def _document_version(document: Any) -> int:
    if not isinstance(document, Mapping):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "stage envelope must be an object"
        )
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "schema_version must be an integer"
        )
    return version


# {
#   責務: [_decode_utf8_text: 入力文書をbyte上限内の厳密なUTF-8文字列にする]
#   処理: [文字列はUTF-8へencodeしてbyte数を測り、bytesは上限確認後にBOMを拒否して厳密decodeする]
#   引数: [data: UTF-8 bytesまたは文字列の文書]
#   戻り値: [str: parse対象の文字列]
#   エラー: [StageEnvelopeError: 上限超過はlimit_exceeded、型違い・BOM・不正UTF-8はmalformed]
# }
def _decode_utf8_text(data: bytes | str) -> str:
    if isinstance(data, str):
        try:
            encoded_size = len(data.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED, "document contains unpaired surrogates"
            ) from error
        if encoded_size > STAGE_ENVELOPE_MAX_ENCODED_BYTES:
            raise StageEnvelopeError(
                StageEnvelopeError.LIMIT_EXCEEDED,
                "stage envelope exceeds the byte limit",
            )
        text = data
    elif isinstance(data, (bytes, bytearray)):
        if len(data) > STAGE_ENVELOPE_MAX_ENCODED_BYTES:
            raise StageEnvelopeError(
                StageEnvelopeError.LIMIT_EXCEEDED,
                "stage envelope exceeds the byte limit",
            )
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as error:
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED, "document is not valid UTF-8"
            ) from error
    else:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "document must be bytes or str"
        )
    if text.startswith(_BYTE_ORDER_MARK):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            "document must not start with a byte order mark",
        )
    return text


# {
#   責務: [_reject_excessive_nesting: parse前に文字列外の`{`/`[`の入れ子深さを数え、深すぎる文書を再帰parseさせない]
#   処理: [文字列literalとescapeを読み飛ばしながら開き括弧で深さを増やし閉じ括弧で減らし、上限超過または対応のない閉じ括弧の時点で拒否する]
#   引数: [text: UTF-8 decode済みのJSON文書]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError: objectと配列の深さがSTAGE_ENVELOPE_MAX_DEPTHを超えればlimit_exceeded、対応のない閉じ括弧はmalformed]
#   補足: [括弧の対応や文法の正しさはここでは判定せず、後続のjson.loadsが判定する]
# }
def _reject_excessive_nesting(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
            if depth > STAGE_ENVELOPE_MAX_DEPTH:
                raise StageEnvelopeError(
                    StageEnvelopeError.LIMIT_EXCEEDED,
                    "document exceeds the nesting depth limit",
                )
        elif character in "}]":
            depth -= 1
            if depth < 0:
                raise StageEnvelopeError(
                    StageEnvelopeError.MALFORMED,
                    "document has an unmatched closing bracket",
                )


# {
#   責務: [_object_without_duplicate_keys: JSON objectの重複keyを拒否し、言語ごとに異なる「後勝ち」解釈を防ぐ]
#   処理: [key・値の組を順に辞書へ入れ、既出keyがあれば拒否する]
#   引数: [pairs: json parserが渡す1 objectのkey・値の組]
#   戻り値: [dict[str, Any]: 出現順を保ったobject]
#   エラー: [StageEnvelopeError(malformed): 同じkeyが2回以上ある場合]
# }
def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED, f"duplicate JSON object key {key!r}"
            )
        result[key] = value
    return result


# {
#   責務: [_reject_non_finite_constant: Python json moduleが拡張として受け付けるNaN・Infinityを拒否する]
#   処理: [呼ばれた時点で、標準JSONにない定数として必ず拒否する]
#   引数: [constant: parserが見つけた`NaN`・`Infinity`・`-Infinity`のいずれか]
#   戻り値: [なし: 常に例外を送出する]
#   エラー: [StageEnvelopeError(malformed): 常に]
# }
def _reject_non_finite_constant(constant: str) -> Any:
    raise StageEnvelopeError(
        StageEnvelopeError.MALFORMED, f"non-standard JSON constant {constant}"
    )
