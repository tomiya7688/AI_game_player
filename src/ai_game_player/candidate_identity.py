from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

from ai_game_player.models import ActionCandidate

MAX_CANDIDATES = 128
MAX_PAIR_COMPARISONS = 16_384

REASON_OK = "ok"
REASON_CAPACITY = "unknown/capacity"
REASON_COLLISION = "collision/same_id_different_target"
REASON_AMBIGUOUS = "unknown/ambiguous_identity"
REASON_INVALID = "rejected/invalid_geometry"


class Grounding(Enum):
    SAME = "same"
    DIFFERENT = "different"
    AMBIGUOUS = "ambiguous"


# {
#   責務: [CandidateIdentity: 統合前の1候補が「どの画面のどの要素か」を比較するための証拠を保持する]
#   フィールド: [action_id: 候補ID, kind: 操作種別, coordinate_space: 座標系名（不明はNone）, bbox: 外接矩形, center: 中心座標, source: 入力元, frame_ref: 画面の参照名（不明はNone）, action_generation: 候補世代（不明はNone）, text: 正規化済み表示文字]
# }
@dataclass(frozen=True)
class CandidateIdentity:
    action_id: str
    kind: str
    bbox: tuple[int, int, int, int] | None
    center: tuple[int, int] | None
    source: str
    coordinate_space: str | None = None
    frame_ref: str | None = None
    action_generation: int | None = None
    text: str = ""


# {
#   責務: [MergeContext: 呼び出し元が明示する座標系・Frame・世代を入力元ごとに保持する]
#   フィールド: [coordinate_spaces: 入力元名から座標系名への対応, frame_refs: 入力元名からFrame参照への対応, generations: 入力元名から候補世代への対応]
#   補足: [未指定の入力元は「単一Frame内・同一座標系・同一世代」とみなす。指定済みで食い違う場合は統合しない]
# }
@dataclass(frozen=True)
class MergeContext:
    coordinate_spaces: dict[str, str] = field(default_factory=dict)
    frame_refs: dict[str, str] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)


# {
#   責務: [CandidateMergeReport: 統合結果と、統合しなかった理由を呼び出し元へ返す]
#   フィールド: [candidates: 最終許可候補, sources: 各候補を検出した入力元, origin_ids: 各候補へ統合した元ID, collisions: 同ID異対象のため退避した候補, rejected: 理由付きで拒否した候補, reason: 全体判定の固定理由, comparisons: 実際に行った比較回数]
# }
@dataclass(frozen=True)
class CandidateMergeReport:
    candidates: tuple[ActionCandidate, ...] = ()
    sources: tuple[tuple[str, ...], ...] = ()
    origin_ids: tuple[tuple[str, ...], ...] = ()
    collisions: tuple[ActionCandidate, ...] = ()
    collision_origin_ids: tuple[tuple[str, ...], ...] = ()
    collision_sources: tuple[tuple[str, ...], ...] = ()
    rejected: tuple[tuple[ActionCandidate, str], ...] = ()
    reason: str = REASON_OK
    comparisons: int = 0


# {
#   責務: [valid_bbox: 矩形が有限な正の大きさを持つか検査する]
#   処理: [4値が有限かつ幅・高さが正であることを確認する]
#   引数: [bbox: 検査する矩形]
#   戻り値: [bool: 利用できる矩形ならTrue]
# }
def valid_bbox(bbox: tuple[int, int, int, int]) -> bool:
    try:
        return (
            len(bbox) == 4
            and all(isfinite(value) for value in bbox)
            and bbox[2] > 0
            and bbox[3] > 0
        )
    except OverflowError:
        # 巨大な整数はfloat変換できないため不正な幾何として扱う
        return False


# {
#   責務: [iou: 2矩形の重なり率を求める]
#   処理: [交差面積を和集合面積で割る。呼び出し前にvalid_bboxで検査済みであること]
#   引数: [left: 矩形, right: 矩形]
#   戻り値: [float: 0以上1以下の重なり率]
# }
def iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    left_x, left_y, left_width, left_height = left
    right_x, right_y, right_width, right_height = right
    overlap_width = max(
        0, min(left_x + left_width, right_x + right_width) - max(left_x, right_x)
    )
    overlap_height = max(
        0, min(left_y + left_height, right_y + right_height) - max(left_y, right_y)
    )
    overlap = overlap_width * overlap_height
    return overlap / (left_width * left_height + right_width * right_height - overlap)


# {
#   責務: [ground_pair: 2候補が同じ画面要素かを、IDではなく証拠で判定する]
#   処理: [座標系・Frame・世代が両方既知で食い違えばAMBIGUOUS、種別違いはDIFFERENT。矩形の重なり率か中心距離が閾値内で、文字が両方あれば一致する場合だけSAME。IDは判定に使わない]
#   引数: [left: 候補, right: 候補, proximity: 中心距離の許容値, iou_threshold: 重なり率の閾値]
#   戻り値: [Grounding: SAME・DIFFERENT・AMBIGUOUSのいずれか]
# }
def ground_pair(
    left: CandidateIdentity,
    right: CandidateIdentity,
    proximity: int = 20,
    iou_threshold: float = 0.5,
) -> Grounding:
    for first, second in (
        (left.coordinate_space, right.coordinate_space),
        (left.frame_ref, right.frame_ref),
        (left.action_generation, right.action_generation),
    ):
        if first is not None and second is not None and first != second:
            return Grounding.AMBIGUOUS
    if left.kind != right.kind:
        return Grounding.DIFFERENT
    text_conflict = bool(left.text and right.text and left.text != right.text)
    if (
        left.bbox is not None
        and right.bbox is not None
        and iou(left.bbox, right.bbox) >= iou_threshold
    ):
        return Grounding.DIFFERENT if text_conflict else Grounding.SAME
    if left.center is None or right.center is None:
        # 位置の証拠がない組（キー操作など）は統合せず別候補として扱う。同IDなら呼び出し側がcollisionにする
        return Grounding.DIFFERENT
    near = (
        abs(left.center[0] - right.center[0]) <= proximity
        and abs(left.center[1] - right.center[1]) <= proximity
    )
    if not near:
        return Grounding.DIFFERENT
    if left.text and right.text:
        return Grounding.DIFFERENT if text_conflict else Grounding.SAME
    return (
        Grounding.SAME
        if "image" in (left.source, right.source)
        else Grounding.DIFFERENT
    )
