"""Render-independent Player Character state and asset pack contract (Issue #155)."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType

DEFAULT_PLAYER_PACK = (
    Path(__file__).resolve().parent / "assets" / "players" / "wise_misk" / "pack.json"
)
FALLBACK_DISPLAY_NAME = "Player"
FALLBACK_MESSAGE = ""
SCHEMA_VERSION = 1
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


# {
#   責務: [PlayerState: Player Seatが表示するPlayer Characterの状態を、描画方法から独立した値として定義する]
#   フィールド: [
#     IDLE: 待機中, OBSERVE: 画面取得中, THINK: 判断中, ACT: 操作実行中,
#     EVALUATE: 結果評価中, UNKNOWN: 状態不明, WARNING: 失敗・要確認, STOP: 停止済み
#   ]
# }
class PlayerState(str, Enum):
    IDLE = "idle"
    OBSERVE = "observe"
    THINK = "think"
    ACT = "act"
    EVALUATE = "evaluate"
    UNKNOWN = "unknown"
    WARNING = "warning"
    STOP = "stop"


# {
#   責務: [PlayerAssetPack: Player Seatへ座るoccupantの表示名・画像・License・状態別メッセージを、UI部品から独立して保持する]
#   フィールド: [
#     asset_pack_id: Asset Packの安定識別子,
#     display_name: Player Seatへ表示するoccupant名,
#     player_image: 通常表示画像のパス。Noneまたは存在しない場合は文字表示へfallbackする,
#     license_name: 一覧へ常時表示するLicense名,
#     license_detail: License全文の参照先,
#     source: 素材の出典,
#     image_scaling: 画像の拡大縮小方式。dot-artのため"nearest"だけを受け付ける,
#     profiles: このPackを既定occupantにできるbuild profile名,
#     state_messages: PlayerStateごとの短い脳内メッセージ。読取専用Mapping
#   ]
# }
@dataclass(frozen=True)
class PlayerAssetPack:
    asset_pack_id: str
    display_name: str
    player_image: Path | None = None
    license_name: str = ""
    license_detail: str = ""
    source: str = ""
    image_scaling: str = "nearest"
    profiles: tuple[str, ...] = ()
    state_messages: Mapping[PlayerState, str] = field(
        default_factory=lambda: MappingProxyType({})
    )

    # {
    #   責務: [message_for: 指定状態の脳内メッセージを返す]
    #   処理: [状態別メッセージがなければUNKNOWNのメッセージ、それもなければ空文字を返す]
    #   引数: [state: 表示したいPlayerState]
    #   戻り値: [str: Player Seatへ表示する短文。空文字は吹き出しを出さない意味]
    # }
    def message_for(self, state: PlayerState) -> str:
        return self.state_messages.get(
            state, self.state_messages.get(PlayerState.UNKNOWN, FALLBACK_MESSAGE)
        )

    # {
    #   責務: [has_image: 通常表示画像を描画に使えるかを判定する]
    #   処理: [画像パスが設定され、通常ファイルとして存在するかを確認する]
    #   引数: []
    #   戻り値: [bool: Trueなら画像を読む。Falseなら表示名による文字fallbackを使う]
    # }
    def has_image(self) -> bool:
        return self.player_image is not None and self.player_image.is_file()


# {
#   責務: [load_asset_pack: Asset Pack manifestを読み、検証済みのPlayerAssetPackを返す]
#   処理: [
#     1: UTF-8 JSONを読み、schema_version 1・必須の識別子・表示名・scaling方式・License/出典の文字列型を検証する
#     2: 画像パスをmanifest配下の相対パスとして解決し、SHA-256が記録されていれば画像の存在と一致を確認する
#     3: 未知の状態名を含むメッセージは拒否する
#   ]
#   引数: [manifest_path: pack.jsonのパス]
#   戻り値: [PlayerAssetPack: 検証済みのAsset Pack]
#   エラー: [ValueError: manifestや画像が読めない、形式が不正、画像がmanifest外、または画像hashが一致しない場合]
# }
def load_asset_pack(manifest_path: Path) -> PlayerAssetPack:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Player Asset Packを読めません: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ValueError("Player Asset Pack manifestはJSON objectである必要があります")
    version = manifest.get("schema_version")
    # JSONのtrueや1.0は==1になるため、型も整数に限定する。
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError(f"未対応のschema_versionです: {version}")
    pack_id = manifest.get("asset_pack_id")
    display_name = manifest.get("display_name")
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise ValueError("asset_pack_idが必要です")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("display_nameが必要です")
    scaling = manifest.get("image_scaling", "nearest")
    if scaling != "nearest":
        raise ValueError(f"未対応のimage_scalingです: {scaling}")

    image_path = _resolve_player_image(manifest_path, manifest)
    raw_messages = manifest.get("state_messages", {})
    if not isinstance(raw_messages, dict):
        raise ValueError("state_messagesはobjectである必要があります")
    messages: dict[PlayerState, str] = {}
    for name, message in raw_messages.items():
        try:
            state = PlayerState(name)
        except ValueError as exc:
            raise ValueError(f"未知のPlayer状態です: {name}") from exc
        if not isinstance(message, str):
            raise ValueError(f"{name}のメッセージは文字列である必要があります")
        messages[state] = message

    profiles = manifest.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
        raise ValueError("profilesは文字列の配列である必要があります")
    return PlayerAssetPack(
        asset_pack_id=pack_id,
        display_name=display_name,
        player_image=image_path,
        license_name=_optional_text(manifest, "license_name"),
        license_detail=_optional_text(manifest, "license_detail"),
        source=_optional_text(manifest, "source"),
        image_scaling=scaling,
        profiles=tuple(profiles),
        state_messages=MappingProxyType(messages),
    )


# {
#   責務: [_resolve_player_image: manifestのplayer_imageをmanifestと同じフォルダー配下のパスへ解決し、記録されたSHA-256を照合する]
#   処理: [
#     1: 未指定ならNoneを返す。文字列以外、空文字、絶対パス、解決後にmanifestのフォルダー外を指すパスは拒否する
#     2: SHA-256が記録されていれば、64桁の16進数であること、画像が読めること、大文字小文字を無視して一致することを確認する
#     3: SHA-256がなければ画像の存在は確認せず、欠落時の文字fallbackを呼び出し側へ任せる
#   ]
#   引数: [manifest_path: pack.jsonのパス, manifest: 読込済みのmanifest object]
#   戻り値: [Path | None: 画像パス。未指定ならNone]
#   エラー: [ValueError: パスまたはhashの形式が不正、画像が読めない、hashが一致しない場合]
# }
def _resolve_player_image(manifest_path: Path, manifest: dict) -> Path | None:
    image_name = manifest.get("player_image")
    if image_name is None:
        return None
    if (
        not isinstance(image_name, str)
        or not image_name
        or Path(image_name).is_absolute()
    ):
        raise ValueError("player_imageはmanifestからの相対パスである必要があります")
    try:
        pack_root = manifest_path.parent.resolve()
        image_path = (pack_root / image_name).resolve()
    except OSError as exc:
        raise ValueError(f"player_imageを解決できません: {image_name}: {exc}") from exc
    if not image_path.is_relative_to(pack_root):
        raise ValueError(f"player_imageがAsset Packの外を指しています: {image_name}")
    expected_hash = manifest.get("player_image_sha256")
    if expected_hash is None:
        return image_path
    if not isinstance(expected_hash, str) or not _SHA256_HEX.fullmatch(expected_hash):
        raise ValueError("player_image_sha256は64桁の16進数である必要があります")
    try:
        actual_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"player_imageを読めません: {image_path}: {exc}") from exc
    if actual_hash != expected_hash.lower():
        raise ValueError(f"player_imageのSHA-256が一致しません: {image_path}")
    return image_path


# {
#   責務: [_optional_text: manifestの任意文字列項目を取り出す]
#   処理: [未指定なら空文字を返し、指定されていれば文字列であることを確認する]
#   引数: [manifest: 読込済みのmanifest object, key: 項目名]
#   戻り値: [str: 項目の値または空文字]
#   エラー: [ValueError: 値が文字列以外（nullを含む）の場合]
# }
def _optional_text(manifest: dict, key: str) -> str:
    value = manifest.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key}は文字列である必要があります")
    return value


# {
#   責務: [load_default_player: Development既定occupantである賢者ミスクのAsset Packを返す]
#   処理: [同梱manifestを読み、失敗した場合は画像なしの汎用Playerへfallbackする]
#   引数: [manifest_path: 読むmanifest。省略時は同梱の賢者ミスク]
#   戻り値: [PlayerAssetPack: 表示に使うAsset Pack。読込失敗でも例外を送出しない]
# }
def load_default_player(manifest_path: Path = DEFAULT_PLAYER_PACK) -> PlayerAssetPack:
    try:
        return load_asset_pack(manifest_path)
    except ValueError:
        return PlayerAssetPack(
            asset_pack_id="fallback.player", display_name=FALLBACK_DISPLAY_NAME
        )


# {
#   責務: [nearest_scale: dot-art画像を目標サイズへ近づけるnearest-neighborの整数倍率を決める]
#   処理: [
#     1: 原画像が目標以下なら、目標を超えない最大の整数拡大倍率を返す（org画像のupscale）
#     2: 原画像が目標より大きければ、目標以下へ収まる最小の整数間引き倍率を返す（1920px版のdownscale）
#   ]
#   引数: [source_px: 原画像の一辺のpixel数, target_px: 表示したい一辺のpixel数]
#   戻り値: [tuple[int, int]: (拡大倍率, 間引き倍率)。どちらも1以上で、少なくとも一方は1]
#   エラー: [ValueError: source_pxまたはtarget_pxが1未満の場合]
# }
def nearest_scale(source_px: int, target_px: int) -> tuple[int, int]:
    if source_px < 1 or target_px < 1:
        raise ValueError("画像サイズは1px以上である必要があります")
    if source_px <= target_px:
        return target_px // source_px, 1
    return 1, -(-source_px // target_px)
