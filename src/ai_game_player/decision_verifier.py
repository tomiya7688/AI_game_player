import re
from typing import Any, Callable, Sequence
from uuid import uuid4

from ai_game_player.decision_context import CandidateDecisionContext, DecisionContext
from ai_game_player.decision_reliability import (
    ReliabilityEvidence as ReliabilityEvidence,
    ReliabilityResult as ReliabilityResult,
    ReliabilityStatus as ReliabilityStatus,
    _decision_fields,
)
from ai_game_player.models import ActionDecision, ScreenObservation

DETERMINISTIC_EVIDENCE_CONFIDENCE = 1.0
JAPANESE_NEGATION_WINDOW_CHARS = 24
JAPANESE_PARTICLE_CHARS = frozenset("をはがにへとでのもやからより")
ENGLISH_ACTION_DIRECTIVE_PATTERN = r"choose|select|pick|use|click|double\s+click|press"
ENGLISH_ACTION_PASSIVE_PATTERN = r"chosen|selected|picked|used|clicked|double\s+clicked|pressed"
ENGLISH_ACTION_OBJECT_PREFIX_PATTERN = r"(?:(?:on\s+)?(?:the|a|an)\s+|on\s+)"
ENGLISH_ACTION_OBJECT_SUFFIX_PATTERN = r"(?:\s+(?:button|key|option|control|item|action|menu|tab|link|icon))?"
JAPANESE_NEGATION_CONTINUATION_PATTERN = (
    r"ではない|ではありません|じゃない|じゃありません|でない|ではなく|でなく|わけではない|"
    r"わけではありません|必要(?:は|が)?(?:ない|ありません)|べきではない|べきではありません|"
    r"とは限らない|とは限りません"
)


# {
#   責務: [
#     DecisionVerifier: Decision出力を現在の候補・snapshot・画面へ決定論的に照合する
#   ]
#   処理: [
#     1: Decision schemaと型を検査する
#     2: 選択候補を許可候補へ照合する
#     3: snapshot・scene・reasonの矛盾を検査する
#     4: 根拠と出所をReliabilityResultへ記録する
#   ]
#   フィールド: []
# }
class DecisionVerifier:
    # {
    #   責務: [
    #     verify: Provider判断をschema・候補・scene・reasonの証拠で検証する
    #   ]
    #   処理: [
    #     1: 出力schemaと型を検査する
    #     2: context候補と現在画面の整合を検査する
    #     3: Evaluator許可候補・snapshot・scene参照とreason/action矛盾を検査する
    #     4: EvidenceとprovenanceをReliabilityResultへまとめる
    #   ]
    #   引数: [
    #     decision_output: Providerから返された未信頼値
    #     context: 判断に使ったDecision Context
    #     observation: 実際の現在観測
    #     evaluator_allowed_action_ids: Action Evaluatorが許可した候補ID
    #   ]
    #   戻り値: [
    #     ReliabilityResult: TRUST・CAUTION・VERIFY・REJECT判定
    #   ]
    #   エラー: []
    # }
    def verify(
        self,
        decision_output: object,
        context: DecisionContext,
        observation: ScreenObservation,
        *,
        evaluator_allowed_action_ids: Sequence[str] | None = None,
    ) -> ReliabilityResult:
        evidence: list[ReliabilityEvidence] = []

        def add_evidence(
            check: str,
            severity: str,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            # {
            #   責務: [
            #     add_evidence: 一貫した出所を付けて検査Evidenceを追加する
            #   ]
            #   処理: [
            #     1: 検査名・深刻度・比較値をReliabilityEvidenceへ変換する
            #     2: 検査結果一覧へ追記する
            #   ]
            #   引数: [
            #     check: 検査名
            #     severity: 検査の深刻度
            #     message: 判定理由
            #     details: 比較値
            #   ]
            #   戻り値: []
            # }
            # 検査ごとに出所と比較値を固定形式で残す。
            evidence.append(
                ReliabilityEvidence(
                    check,
                    severity,
                    DETERMINISTIC_EVIDENCE_CONFIDENCE,
                    message,
                    details=details or {},
                )
            )

        # Invalid types, missing required fields, and provider parser errors are rejected immediately.
        decision_fields = _decision_fields(decision_output)
        if decision_fields is None:
            schema_details = {}
            if isinstance(decision_output, ActionDecision) and decision_output.validation_error:
                schema_details["provider_validation_error"] = decision_output.validation_error
            add_evidence(
                "decision_schema",
                "reject",
                "Decision schema or field type is invalid",
                schema_details,
            )
            provider = (
                decision_output.provider
                if isinstance(decision_output, ActionDecision) and isinstance(decision_output.provider, str)
                else ""
            )
            return self._build_result(context, "", provider, evidence)

        # Context自体が現在の観測を表しているかを先に検証する。
        state_screen_id = context.state.get("screen_id")
        if state_screen_id != observation.screen_id:
            add_evidence(
                "context_scene_consistency",
                "reject",
                "Decision Context screen does not match the current observation",
                {"context_screen_id": state_screen_id, "observation_screen_id": observation.screen_id},
            )
        expected_size = [observation.width, observation.height]
        if context.state.get("size") != expected_size:
            add_evidence(
                "context_scene_consistency",
                "reject",
                "Decision Context dimensions do not match the current observation",
                {"context_size": context.state.get("size"), "observation_size": expected_size},
            )
        context_signature = context.state.get("signature")
        observation_signature = observation.features.get("signature", "")
        if context_signature and observation_signature and context_signature != observation_signature:
            add_evidence(
                "context_scene_consistency",
                "reject",
                "Decision Context signature does not match the current observation",
                {"context_signature": context_signature, "observation_signature": observation_signature},
            )
        elif not context_signature or not observation_signature:
            add_evidence(
                "scene_signature_unavailable",
                "verify",
                "A scene signature is unavailable for consistency verification",
            )

        # Each selected ID must resolve to one candidate that the current snapshot explicitly allows.
        candidate_by_id = self._index_candidates(context, add_evidence)
        allowed_ids = list(context.allowed_action_ids)
        evaluator_allowed_ids = (
            list(evaluator_allowed_action_ids)
            if evaluator_allowed_action_ids is not None
            else allowed_ids
        )
        if not allowed_ids:
            add_evidence("allowed_candidate_grounding", "reject", "Decision Context has no allowed actions")
        elif len(allowed_ids) != len(set(allowed_ids)):
            add_evidence("allowed_candidate_grounding", "reject", "Decision Context repeats an allowed action ID")
        if evaluator_allowed_action_ids is not None:
            if not evaluator_allowed_ids:
                add_evidence("allowed_candidate_grounding", "reject", "Action Evaluator has no allowed actions")
            elif len(evaluator_allowed_ids) != len(set(evaluator_allowed_ids)):
                add_evidence("allowed_candidate_grounding", "reject", "Action Evaluator repeats an allowed action ID")

        selected_candidate = candidate_by_id.get(decision_fields.action_id)
        if (
            decision_fields.action_id not in allowed_ids
            or decision_fields.action_id not in evaluator_allowed_ids
            or selected_candidate is None
            or not selected_candidate.allowed
        ):
            grounding_details = {
                "action_id": decision_fields.action_id,
                "allowed_action_ids": allowed_ids,
            }
            if evaluator_allowed_action_ids is not None:
                grounding_details["evaluator_allowed_action_ids"] = evaluator_allowed_ids
            add_evidence(
                "allowed_candidate_grounding",
                "reject",
                "Selected action is not grounded in an allowed candidate",
                grounding_details,
            )
        else:
            add_evidence(
                "allowed_candidate_grounding",
                "info",
                "Selected action matches one allowed candidate in the current context",
                {"action_id": decision_fields.action_id},
            )

        # Echoed references bind the provider answer to the exact prompt state.
        if not context.snapshot_id:
            add_evidence("snapshot_reference", "reject", "Decision Context has no snapshot ID")
        elif not decision_fields.snapshot_id:
            add_evidence(
                "snapshot_reference",
                "verify",
                "Decision output does not echo the current snapshot ID",
                {"expected_snapshot_id": context.snapshot_id},
            )
        elif decision_fields.snapshot_id != context.snapshot_id:
            add_evidence(
                "snapshot_reference",
                "reject",
                "Decision output references a stale or different snapshot",
                {"expected_snapshot_id": context.snapshot_id, "received_snapshot_id": decision_fields.snapshot_id},
            )
        else:
            add_evidence(
                "snapshot_reference",
                "info",
                "Decision output references the current snapshot",
                {"snapshot_id": context.snapshot_id},
            )

        if not decision_fields.screen_id:
            add_evidence("scene_reference", "verify", "Decision output does not echo the current screen ID")
        elif decision_fields.screen_id != state_screen_id:
            add_evidence(
                "scene_reference",
                "reject",
                "Decision output references a different screen",
                {"expected_screen_id": state_screen_id, "received_screen_id": decision_fields.screen_id},
            )
        else:
            add_evidence("scene_reference", "info", "Decision output references the current screen")

        if not decision_fields.state_signature:
            add_evidence("state_reference", "verify", "Decision output does not echo the current state signature")
        elif decision_fields.state_signature != context_signature:
            add_evidence(
                "state_reference",
                "reject",
                "Decision output references a different state signature",
                {"expected_signature": context_signature, "received_signature": decision_fields.state_signature},
            )
        else:
            add_evidence("state_reference", "info", "Decision output references the current state signature")

        # Only explicit negation of the selected candidate is treated as contradiction.
        if not decision_fields.reason.strip():
            add_evidence("reason_action_consistency", "caution", "Decision reason is empty")
        elif selected_candidate is not None and _reason_contradicts_action(
            decision_fields.reason,
            selected_candidate,
            context.candidates,
        ):
            add_evidence(
                "reason_action_consistency",
                "reject",
                "Decision reason explicitly advises against the selected action",
                {"action_id": selected_candidate.action_id, "label": selected_candidate.label},
            )
        else:
            add_evidence("reason_action_consistency", "info", "Decision reason does not contradict the selected action")

        return self._build_result(
            context,
            decision_fields.action_id,
            decision_fields.provider,
            evidence,
        )

    # {
    #   責務: [
    #     normalize_decision: 検証を通過したMapping出力をActionDecisionへ変換する
    #   ]
    #   処理: [
    #     1: Decisionの型と必須値を検証する
    #     2: 有効な値だけActionDecisionへ格納する
    #   ]
    #   引数: [
    #     decision_output: Providerから返された未信頼の値
    #   ]
    #   戻り値: [
    #     ActionDecision | None: 正規化結果、または不正時のNone
    #   ]
    # }
    @staticmethod
    def normalize_decision(decision_output: object) -> ActionDecision | None:
        decision_fields = _decision_fields(decision_output)
        if decision_fields is None:
            return None
        return ActionDecision(
            decision_fields.action_id,
            decision_fields.reason,
            decision_fields.provider,
            decision_fields.snapshot_id,
            decision_fields.screen_id,
            decision_fields.state_signature,
        )

    # {
    #   責務: [
    #     _index_candidates: Context内候補を一意なaction IDで検索できるようにする
    #   ]
    #   処理: [
    #     1: 候補IDの重複を検査する
    #     2: allowedフラグと許可ID一覧の整合を検査する
    #     3: 候補IDから候補へ対応付ける
    #   ]
    #   引数: [
    #     context: 検証対象Decision Context
    #     add_evidence: 検査Evidenceを追加する関数
    #   ]
    #   戻り値: [
    #     dict[str, CandidateDecisionContext]: action IDから候補への対応
    #   ]
    # }
    @staticmethod
    def _index_candidates(
        context: DecisionContext,
        add_evidence: Callable[[str, str, str, dict[str, Any] | None], None],
    ) -> dict[str, CandidateDecisionContext]:
        candidates_by_id: dict[str, CandidateDecisionContext] = {}
        duplicate_ids: set[str] = set()
        allowed_ids = set(context.allowed_action_ids)
        for candidate in context.candidates:
            if candidate.action_id in candidates_by_id:
                duplicate_ids.add(candidate.action_id)
            candidates_by_id[candidate.action_id] = candidate
            if candidate.allowed != (candidate.action_id in allowed_ids):
                add_evidence(
                    "context_candidate_consistency",
                    "reject",
                    "Candidate allowed flag disagrees with the allowed action list",
                    {"action_id": candidate.action_id, "candidate_allowed": candidate.allowed},
                )
        if duplicate_ids:
            add_evidence(
                "context_candidate_consistency",
                "reject",
                "Decision Context contains duplicate candidate IDs",
                {"duplicate_action_ids": sorted(duplicate_ids)},
            )
        missing_ids = allowed_ids.difference(candidates_by_id)
        if missing_ids:
            add_evidence(
                "context_candidate_consistency",
                "reject",
                "Allowed action IDs are missing from the candidate context",
                {"missing_action_ids": sorted(missing_ids)},
            )
        return candidates_by_id

    # {
    #   責務: [
    #     _build_result: Evidenceの深刻度から最終ReliabilityResultを構築する
    #   ]
    #   処理: [
    #     1: reject・verify・cautionの優先順位で状態を決める
    #     2: 判定IDと対象参照を結果へ格納する
    #   ]
    #   引数: [
    #     context: 判定対象Decision Context
    #     action_id: 選択候補ID
    #     provider: 決定Provider
    #     evidence: 決定論的な検査結果
    #   ]
    #   戻り値: [
    #     ReliabilityResult: schema付き信頼性判定
    #   ]
    # }
    @staticmethod
    def _build_result(
        context: DecisionContext,
        action_id: str,
        provider: str,
        evidence: list[ReliabilityEvidence],
    ) -> ReliabilityResult:
        severities = {item.severity for item in evidence}
        if "reject" in severities:
            status = ReliabilityStatus.REJECT
        elif "verify" in severities:
            status = ReliabilityStatus.VERIFY
        elif "caution" in severities:
            status = ReliabilityStatus.CAUTION
        else:
            status = ReliabilityStatus.TRUST
        return ReliabilityResult(uuid4().hex, status, context.snapshot_id, action_id, provider, tuple(evidence))


# {
#   責務: [
#     _reason_contradicts_action: 理由が選択・クリック・押下を明示的に拒否しているか検査する
#   ]
#   処理: [
#     1: 理由・action ID・候補名の区切り文字を同じ形式へ正規化する
#     2: 英語・日本語の長い別候補名に一致する理由を区別する
#     3: 後続修飾語の長さに制限を設けず、選択・入力を否定する日本語・英語表現を照合する
#   ]
#   引数: [
#     reason: Providerが返した選択理由
#     candidate: 選択された許可候補
#     candidates: 同じ画面にある候補の一覧
#   ]
#   戻り値: [
#     bool: 明示的な理由・Action矛盾があればTrue
#   ]
# }
def _reason_contradicts_action(
    reason: str,
    candidate: CandidateDecisionContext,
    candidates: Sequence[CandidateDecisionContext],
) -> bool:
    normalized_terms = {
        _normalize_contradiction_text(value)
        for value in (candidate.action_id, candidate.label)
        if value.strip()
    }
    all_candidate_terms = {
        _normalize_contradiction_text(value)
        for item in candidates
        for value in (item.action_id, item.label)
        if value.strip()
    }
    normalized_reason = _normalize_contradiction_text(reason)
    display_reason = " ".join(reason.replace("_", " ").replace("-", " ").split())
    for term in normalized_terms:
        escaped_term = re.escape(term)
        bounded_english_term = rf"(?<!\w){escaped_term}(?!\w)"
        english_target = (
            rf"(?:{ENGLISH_ACTION_OBJECT_PREFIX_PATTERN})?"
            rf"(?P<candidate_term>{bounded_english_term})"
            rf"{ENGLISH_ACTION_OBJECT_SUFFIX_PATTERN}"
        )
        active_negation_prefixes = (
            rf"\b(?:do not|don't|should not|must not|not)\s+(?:(?:{ENGLISH_ACTION_DIRECTIVE_PATTERN})\s+)?",
            rf"\b(?:avoid|reject)\s+(?:(?:{ENGLISH_ACTION_DIRECTIVE_PATTERN})\s+)?",
        )
        for prefix in active_negation_prefixes:
            for match in re.finditer(prefix + english_target, display_reason, flags=re.IGNORECASE):
                candidate_term_start = match.start("candidate_term")
                if not _matches_longer_candidate_phrase(
                    display_reason,
                    candidate_term_start,
                    term,
                    all_candidate_terms,
                ):
                    return True

        passive_negation = (
            rf"(?P<candidate_term>{bounded_english_term})"
            rf"{ENGLISH_ACTION_OBJECT_SUFFIX_PATTERN}"
            rf"\s+(?:should|must)\s+not\s+be\s+(?:{ENGLISH_ACTION_PASSIVE_PATTERN})\b"
        )
        for match in re.finditer(passive_negation, display_reason, flags=re.IGNORECASE):
            candidate_term_start = match.start("candidate_term")
            if not _matches_longer_candidate_phrase(
                display_reason,
                candidate_term_start,
                term,
                all_candidate_terms,
            ):
                return True

        japanese_directives = r"選ばない|選択しない|使わない|避ける|拒否する|不適切"
        japanese_directive = (
            rf"(?P<directive_before>{japanese_directives})"
            rf".{{0,{JAPANESE_NEGATION_WINDOW_CHARS}}}(?P<target_after>{escaped_term})"
            rf"|(?P<target_before>{escaped_term}).{{0,{JAPANESE_NEGATION_WINDOW_CHARS}}}"
            rf"(?:は|を|が)?(?P<directive_after>{japanese_directives})"
        )
        japanese_negation = (
            rf"(?=(?:{japanese_directive})(?!{JAPANESE_NEGATION_CONTINUATION_PATTERN}))"
        )
        for match in re.finditer(japanese_negation, normalized_reason):
            if match.group("target_after") is not None:
                target_start = match.start("target_after")
                directive_start = match.start("directive_before")
                if _matches_longer_japanese_candidate(
                    normalized_reason,
                    target_start,
                    term,
                    all_candidate_terms,
                ):
                    continue
                if _mentions_longer_candidate_before_japanese_negation(
                    normalized_reason,
                    directive_start,
                    term,
                    all_candidate_terms,
                ):
                    continue
            else:
                target_start = match.start("target_before")
                if _matches_longer_japanese_candidate(
                    normalized_reason,
                    target_start,
                    term,
                    all_candidate_terms,
                ):
                    continue
            return True
    return False


# {
#   責務: [
#     _matches_longer_candidate_phrase: 選択候補名より長い別の候補名を理由が指しているか調べる
#   ]
#   処理: [
#     1: 選択候補名の後ろに語が続く別候補名を探す
#     2: 現在の候補一覧にある長い候補名と理由を照合する
#     3: 理由が別候補名を指している場合Trueを返す
#   ]
#   引数: [
#     reason: 区切りだけ正規化し大文字小文字を残した判断理由
#     candidate_term_start: 候補名の照合開始位置
#     selected_term: 選択候補の正規化済みIDまたは名称
#     candidate_terms: 現在画面にある候補の正規化済みIDと名称
#   ]
#   戻り値: [
#     bool: 別候補名全体との一致がある場合True
#   ]
# }
def _matches_longer_candidate_phrase(
    reason: str,
    candidate_term_start: int,
    selected_term: str,
    candidate_terms: set[str],
) -> bool:
    remaining_reason = reason[candidate_term_start:]
    for other_term in candidate_terms:
        if not other_term.startswith(f"{selected_term} "):
            continue
        if re.match(rf"{re.escape(other_term)}(?!\w)", remaining_reason, flags=re.IGNORECASE):
            return True
    return False


# {
#   責務: [
#     _matches_longer_japanese_candidate: 日本語理由内で選択候補より長い候補名を照合する
#   ]
#   処理: [
#     1: 選択候補名から始まる候補を一覧から探す
#     2: 候補名の直後が日本語助詞または区切り文字であることを確認する
#     3: 長い別候補名と理由が一致する場合Trueを返す
#   ]
#   引数: [
#     reason: 小文字・区切り文字を正規化した日本語理由
#     candidate_term_start: 候補名の照合開始位置
#     selected_term: 選択候補の正規化済みIDまたは名称
#     candidate_terms: 現在画面にある候補の正規化済みIDと名称
#   ]
#   戻り値: [
#     bool: 長い別候補名を助詞境界まで含めて指している場合True
#   ]
#   エラー: []
# }
def _matches_longer_japanese_candidate(
    reason: str,
    candidate_term_start: int,
    selected_term: str,
    candidate_terms: set[str],
) -> bool:
    remaining_reason = reason[candidate_term_start:]
    for other_term in candidate_terms:
        if not other_term.startswith(selected_term) or len(other_term) <= len(selected_term):
            continue
        if not remaining_reason.startswith(other_term):
            continue
        following_text = remaining_reason[len(other_term) :]
        if (
            not following_text
            or not following_text[0].isalnum()
            or following_text[0] in JAPANESE_PARTICLE_CHARS
        ):
            return True
    return False


# {
#   責務: [
#     _mentions_longer_candidate_before_japanese_negation: 日本語の否定語より前に長い別候補があるか調べる
#   ]
#   処理: [
#     1: 否定語の直前24文字から現在候補より長い候補を探す
#     2: 長い候補名の直後に助詞と否定語が続く構文を照合する
#     3: 否定語が別候補を指す場合Trueを返す
#   ]
#   引数: [
#     reason: 小文字・区切り文字を正規化した日本語理由
#     directive_start: 否定語の開始位置
#     selected_term: 選択候補の正規化済みIDまたは名称
#     candidate_terms: 現在画面にある候補の正規化済みIDと名称
#   ]
#   戻り値: [
#     bool: 否定語の直前で長い別候補を指している場合True
#   ]
#   エラー: []
# }
def _mentions_longer_candidate_before_japanese_negation(
    reason: str,
    directive_start: int,
    selected_term: str,
    candidate_terms: set[str],
) -> bool:
    for other_term in candidate_terms:
        if not other_term.startswith(selected_term) or len(other_term) <= len(selected_term):
            continue
        preceding_text_start = max(0, directive_start - JAPANESE_NEGATION_WINDOW_CHARS - len(other_term))
        preceding_text = reason[preceding_text_start:directive_start]
        candidate_with_particle = rf"{re.escape(other_term)}(?:を|は|が|に|へ|と|で|の|も)$"
        if re.search(candidate_with_particle, preceding_text):
            return True
    return False


# {
#   責務: [
#     _normalize_contradiction_text: 否定照合で使う文字列の大小文字と区切り文字を統一する
#   ]
#   処理: [
#     1: 大小文字をcasefoldで統一する
#     2: ハイフン・アンダースコアを空白へ置換する
#     3: 連続空白を単一空白へ正規化する
#   ]
#   引数: [
#     value: Provider理由または候補名
#   ]
#   戻り値: [
#     str: 正規化した照合文字列
#   ]
# }
def _normalize_contradiction_text(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())
