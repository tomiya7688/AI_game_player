"""画素や予測を保持せず、同じproducerから新しく取得したFrameを照合する。"""

from dataclasses import dataclass

from ai_game_player.models import ScreenObservation


# {
#   責務: [ObservationFreshness: 実capture metadataの有界な順序参照を固定する]
#   フィールド: [source_id: 取得元identity, frame_id: producerが発行したopaque参照, source_generation: source切替世代,
#     captured_at_ns: 同一process monotonic取得時刻。画面状態や成功確率ではない]
# }
@dataclass(frozen=True, slots=True)
class ObservationFreshness:
    source_id: str
    frame_id: str
    source_generation: int
    captured_at_ns: int

    # {
    #   責務: [__post_init__: producer参照と時刻を有限の型付き値へ限定する]
    #   処理: [bool・空参照・過大ID・非正時刻を拒否する]
    #   引数: [self: capture参照] 戻り値: [なし] エラー: [ValueError: 型/容量違反]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, str)
            or not self.source_id
            or len(self.source_id) > 256
            or not isinstance(self.frame_id, str)
            or not self.frame_id
            or len(self.frame_id) > 512
            or type(self.source_generation) is not int
            or self.source_generation < 0
            or type(self.captured_at_ns) is not int
            or self.captured_at_ns <= 0
        ):
            raise ValueError("invalid observation capture reference")

    # {
    #   責務: [from_observation: 実Sourceが付けたcapture参照だけを取り出す]
    #   処理: [欠落・不正を拒否し、画素signatureを新Frame証拠へ代用しない]
    #   引数: [observation: Sourceの実観測] 戻り値: [ObservationFreshness: 有界参照]
    #   エラー: [ValueError: metadata不足]
    # }
    @classmethod
    def from_observation(cls, observation: ScreenObservation) -> "ObservationFreshness":
        metadata = observation.features.get("capture_metadata")
        if not isinstance(metadata, dict):
            raise ValueError("fresh capture metadata is unavailable")
        frame = metadata.get("frame_id")
        source = metadata.get("source_id")
        generation, captured = (
            metadata.get("source_generation"),
            metadata.get("monotonic_ns"),
        )
        if (
            not isinstance(frame, str)
            or not frame
            or len(frame) > 512
            or type(generation) is not int
            or generation < 0
            or type(captured) is not int
            or captured <= 0
        ):
            raise ValueError("invalid fresh capture metadata")
        if source is None:
            parts = frame.rsplit(":", 2)
            if len(parts) == 3 and parts[1] == str(generation) and parts[2].isdigit():
                source = parts[0]
        if not isinstance(source, str) or not source or len(source) > 256:
            raise ValueError("capture source identity is unavailable")
        return cls(source, frame, generation, captured)

    # {
    #   責務: [is_newer_than: source世代をまたがず新しく取得したFrameか照合する]
    #   処理: [ID変更と取得時刻増加の双方を要求する] 引数: [previous: 前のcapture参照]
    #   戻り値: [bool: cachedFrame再利用ならFalse]
    # }
    def is_newer_than(self, previous: "ObservationFreshness") -> bool:
        return (
            self.source_id == previous.source_id
            and self.source_generation == previous.source_generation
            and self.frame_id != previous.frame_id
            and self.captured_at_ns > previous.captured_at_ns
        )
