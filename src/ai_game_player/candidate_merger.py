from collections.abc import Sequence
from math import isfinite

from ai_game_player.candidate_identity import (
    MAX_CANDIDATES,
    REASON_AMBIGUOUS,
    REASON_CAPACITY,
    REASON_COLLISION,
    REASON_INVALID,
    REASON_OK,
    CandidateIdentity,
    CandidateMergeReport,
    Grounding,
    MergeContext,
    ground_pair,
    valid_bbox,
)

from ai_game_player.models import ActionCandidate, DetectedElement


CandidateInput = ActionCandidate | DetectedElement
CandidateView = tuple[ActionCandidate, str, str | None]


class CandidateMerger:
    def __init__(self, proximity: int = 20, iou_threshold: float = 0.5) -> None:
        if not 0 <= iou_threshold <= 1:
            raise ValueError("IoU threshold must be between 0 and 1")
        self.proximity = proximity
        self.iou_threshold = iou_threshold

    # {
    #   責務: [merge: 自動操作・認識文字・画像由来の候補を重複排除して判断一覧へ統合する]
    #   処理: [候補の優先順位と確信度に従って代表候補を選び、入力元を含まない従来形式の一覧を返す]
    #   引数: [automation: 利用者設定または自動操作から得た候補, ocr: 文字認識結果, image: 画像解析結果]
    #   戻り値: [list[ActionCandidate]: 同じ画面要素を重複させず代表候補だけを含む一覧]
    # }
    def merge(
        self,
        automation: list[ActionCandidate],
        ocr: Sequence[CandidateInput],
        image: Sequence[CandidateInput] | None = None,
    ) -> list[ActionCandidate]:
        return [
            candidate
            for candidate, _sources in self.merge_with_sources(automation, ocr, image)
        ]

    # {
    #   責務: [merge_with_sources: 候補を統合し、代表候補に寄与した入力元も呼び出し元へ返す]
    #   処理: [merge_detailedの結果から、衝突・拒否を除いた代表候補と入力元の組だけを返す従来形式のアダプタ]
    #   引数: [automation: 利用者設定または自動操作から得た候補, ocr: 文字認識結果, image: 画像解析結果]
    #   戻り値: [list[tuple[ActionCandidate, tuple[str, ...]]]: 代表候補とその候補を検出した入力元の組]
    # }
    def merge_with_sources(
        self,
        automation: list[ActionCandidate],
        ocr: Sequence[CandidateInput],
        image: Sequence[CandidateInput] | None = None,
    ) -> list[tuple[ActionCandidate, tuple[str, ...]]]:
        report = self.merge_detailed(automation, ocr, image)
        return list(zip(report.candidates, report.sources))

    # {
    #   責務: [merge_detailed: 同一IDの無条件統合を行わず、対象の一致を証拠で確認して候補を統合する]
    #   処理: [入力を検証し、ground_pairで同一と判定した組だけ統合する。同じIDが複数の異なる対象に残る場合は全て衝突として退避し許可候補から外す。世代・座標系が食い違う候補と不正な矩形は理由付きで拒否する。件数が上限を超えたら全候補を保留する]
    #   引数: [automation: 利用者設定または自動操作から得た候補, ocr: 文字認識結果, image: 画像解析結果, context: 入力元ごとの座標系・Frame・世代（省略時は単一Frame内とみなす）]
    #   戻り値: [CandidateMergeReport: 許可候補・入力元・元ID・衝突・拒否・固定理由・比較回数]
    # }
    def merge_detailed(
        self,
        automation: list[ActionCandidate],
        ocr: Sequence[CandidateInput],
        image: Sequence[CandidateInput] | None = None,
        context: MergeContext | None = None,
    ) -> CandidateMergeReport:
        context = context or MergeContext()
        image = image or []
        if len(automation) + len(ocr) + len(image) > MAX_CANDIDATES:
            # 上限超過は無制限のO(n^2)比較を避け、全候補を保留する
            return CandidateMergeReport(reason=REASON_CAPACITY)
        result: list[tuple[CandidateView, set[str], list[str]]] = []
        # 統合済みグループの全メンバーの証拠（代表だけでなく全員と照合するため）
        members: list[list[CandidateIdentity]] = []
        rejected: list[tuple[ActionCandidate, str]] = []
        comparisons = 0
        groups = (("automation", automation), ("ocr", ocr), ("image", image))
        for group, candidates in groups:
            for value in candidates:
                incoming = self._view(value, group)
                if not self._geometry_is_valid(incoming[0]):
                    rejected.append((incoming[0], REASON_INVALID))
                    continue
                incoming_identity = self._identity(incoming, context)
                duplicate = None
                ambiguous = False
                for index in range(len(result)):
                    grounding = Grounding.DIFFERENT
                    for member in members[index]:
                        comparisons += 1
                        pair = ground_pair(
                            member,
                            incoming_identity,
                            self.proximity,
                            self.iou_threshold,
                        )
                        if pair is Grounding.SAME:
                            grounding = pair
                            break
                        if pair is Grounding.AMBIGUOUS:
                            grounding = pair
                    if grounding is Grounding.SAME:
                        duplicate = index
                        break
                    ambiguous = ambiguous or grounding is Grounding.AMBIGUOUS
                if duplicate is None:
                    if ambiguous:
                        rejected.append((incoming[0], REASON_AMBIGUOUS))
                        continue
                    result.append((incoming, {group}, [incoming[0].action_id]))
                    members.append([incoming_identity])
                    continue
                existing, existing_sources, existing_ids = result[duplicate]
                ids = (
                    existing_ids
                    if incoming[0].action_id in existing_ids
                    else [*existing_ids, incoming[0].action_id]
                )
                representative = (
                    incoming if self._prefer(incoming, existing) else existing
                )
                result[duplicate] = (representative, existing_sources | {group}, ids)
                members[duplicate].append(incoming_identity)
        # 代表IDだけでなく統合された全元IDで衝突を判定する
        counts: dict[str, int] = {}
        for _view, _sources, ids in result:
            for action_id in set(ids) | {_view[0].action_id}:
                counts[action_id] = counts.get(action_id, 0) + 1
        colliding = [
            entry
            for entry in result
            if any(
                counts[action_id] > 1
                for action_id in set(entry[2]) | {entry[0][0].action_id}
            )
        ]
        collisions = tuple(view[0] for view, _s, _i in colliding)
        kept = [entry for entry in result if entry not in colliding]
        source_order = ("automation", "ocr", "image")
        return CandidateMergeReport(
            candidates=tuple(view[0] for view, _s, _i in kept),
            sources=tuple(
                tuple(name for name in source_order if name in sources)
                for _v, sources, _i in kept
            ),
            origin_ids=tuple(tuple(ids) for _v, _s, ids in kept),
            collisions=collisions,
            collision_origin_ids=tuple(tuple(ids) for _v, _s, ids in colliding),
            collision_sources=tuple(
                tuple(name for name in source_order if name in sources)
                for _v, sources, _i in colliding
            ),
            rejected=tuple(rejected),
            reason=self._report_reason(collisions, rejected),
            comparisons=comparisons,
        )

    # {"責務": "報告全体のreasonを衝突>幾何不正>曖昧>okの優先順で決める", "処理": "衝突があれば衝突、棄却に不正幾何があれば不正幾何、棄却のみなら曖昧、なければok", "引数": ["collisions: 衝突候補", "rejected: (候補, 理由)の棄却一覧"], "戻り値": ["reason文字列"]}
    @staticmethod
    def _report_reason(
        collisions: tuple[ActionCandidate, ...],
        rejected: list[tuple[ActionCandidate, str]],
    ) -> str:
        if collisions:
            return REASON_COLLISION
        if any(reason == REASON_INVALID for _c, reason in rejected):
            return REASON_INVALID
        return REASON_AMBIGUOUS if rejected else REASON_OK

    def _geometry_is_valid(self, candidate: ActionCandidate) -> bool:
        if candidate.bbox is not None and not valid_bbox(candidate.bbox):
            return False
        try:
            return all(
                value is None or isfinite(value) for value in (candidate.x, candidate.y)
            )
        except OverflowError:
            # 巨大な整数座標はfloat変換できないため不正な幾何として扱う
            return False

    def _identity(
        self, view: CandidateView, context: MergeContext
    ) -> CandidateIdentity:
        candidate, group, text = view
        center = (
            None
            if candidate.x is None or candidate.y is None
            else (candidate.x, candidate.y)
        )
        return CandidateIdentity(
            candidate.action_id,
            candidate.kind,
            candidate.bbox,
            center,
            group,
            context.coordinate_spaces.get(group),
            context.frame_refs.get(group),
            context.generations.get(group),
            self._normalize_text(text),
        )

    def _view(self, value: CandidateInput, group: str) -> CandidateView:
        if isinstance(value, DetectedElement):
            left, top, width, height = value.bbox
            candidate = ActionCandidate(
                value.element_id,
                value.kind,
                value.text or value.element_type,
                left + width // 2,
                top + height // 2,
                value.confidence,
                value.dangerous,
                value.bbox,
            )
            return candidate, group, value.text
        text = None if group == "image" else value.label
        return value, group, text

    def _prefer(self, incoming: CandidateView, existing: CandidateView) -> bool:
        incoming_candidate, incoming_group, _ = incoming
        existing_candidate, existing_group, _ = existing
        incoming_priority = self._priority(incoming_group)
        existing_priority = self._priority(existing_group)
        if incoming_priority != existing_priority:
            return incoming_priority > existing_priority
        return incoming_candidate.confidence > existing_candidate.confidence

    def _priority(self, group: str) -> int:
        return {"automation": 3, "ocr": 2, "image": 1}.get(group, 0)

    def _normalize_text(self, text: str | None) -> str:
        return " ".join(text.casefold().split()) if text else ""
