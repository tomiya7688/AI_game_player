import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ai_game_player.capture_region import CaptureRegion

# {
#   責務: [AppConfig: 判断・実行設定と永続化可能なcapture regionを保持する]
#   フィールド: [provider/model/endpoint/personality/purpose: 判断設定, live_execution/input_mode: 実入力設定, capture_region: screen/window ROI設定]
# }
@dataclass
class AppConfig:
    provider: str = "ローカル規則"
    model: str = "gemma3:4b"
    endpoint: str = "http://127.0.0.1:11434"
    personality: str = "好奇心旺盛"
    purpose: str = "画面の役割を理解する"
    live_execution: bool = False
    input_mode: str = "window_message"
    capture_region: CaptureRegion = field(default_factory=CaptureRegion)

# {
#   責務: [ConfigStore: config.jsonの読込・互換既定値・入力検証・JSON保存を担当する]
#   フィールド: [path: 利用者設定を保存するJSONファイル]
# }
class ConfigStore:

    # {
    #   責務: [__init__: 読み書き対象の設定JSONファイルを保持する]
    #   引数: [path: 利用者設定の保存先]
    #   戻り値: []
    # }
    def __init__(self, path: Path) -> None:
        self.path = path

    # {
    #   責務: [load: 保存済み設定を読み、旧configにないcapture regionにはwindow全体を適用する]
    #   引数: []
    #   戻り値: [AppConfig: 検証済みのアプリ設定]
    #   エラー: [JSON objectでない、capture regionが不正: 読者が設定を修正できる理由を示して送出]
    # }
    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("config.json must contain an object")

        defaults = asdict(AppConfig())
        defaults.pop("capture_region")
        defaults.update({key: value[key] for key in defaults if key in value})
        raw_live = defaults["live_execution"]
        if isinstance(raw_live, str):
            defaults["live_execution"] = raw_live.strip().lower() in {"1", "true", "yes", "on"}
        else:
            defaults["live_execution"] = bool(raw_live)
        if defaults["input_mode"] not in {"window_message", "mouse"}:
            defaults["input_mode"] = "window_message"
        try:
            capture_region = CaptureRegion.from_dict(
                value.get("capture_region", CaptureRegion().to_dict())
            )
        except ValueError as error:
            raise ValueError(f"config.json capture_region is invalid: {error}") from error
        return AppConfig(**defaults, capture_region=capture_region)

    # {
    #   責務: [save: AppConfigをcapture modeとROIを含むUTF-8 JSONへ保存する]
    #   引数: [config: 保存対象の型検証済みアプリ設定]
    #   戻り値: []
    #   エラー: [保存先directory作成またはJSON書込失敗: OS errorを呼び出し元へ送出]
    # }
    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = asdict(config)
        value["capture_region"] = config.capture_region.to_dict()
        self.path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
