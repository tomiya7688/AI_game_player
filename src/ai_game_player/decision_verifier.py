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
#     DecisionVerifier: 判断元の選択が現在の画面で許可された候補に基づくか、実行規則だけで照合する
#   ]
#   処理: [
#     1: 回答の必須項目・型・空値を検査し、不正な形式を後続へ渡さない
#     2: 選ばれた操作IDが画面候補と両方の許可一覧に存在するか照合する
#     3: 回答が参照した画面状態と、選択理由が候補に矛盾していないか調べる
#     4: 各照合の比較内容を記録し、総合状態とともに返す
#   ]
# }
class DecisionVerifier:
    # {
    #   責務: [
    #     verify: 判断元の回答を現在の画面・許可候補・選択理由と照合し、実行前の扱いを決める
    #   ]
    #   処理: [
    #     1: 判断元が返した値の項目名・型・必須値を検査し、形式不備の回答を拒否する
    #     2: 判断材料に記録された候補と、実際の現在画面との食い違いを調べる
    #     3: 候補の許可状態、画面ID・状態ID、選択理由と操作の明示的な矛盾を照合する
    #     4: 検査ごとの期待値・受信値・結論を根拠として集約し、扱いを返す
    #   ]
    #   引数: [
    #     decision_output: 判断元が返した未検証の値。形式や内容が正しいとは仮定しない
    #     context: 判断元へ提示した画面状態、候補一覧、許可候補、過去の結果
    #     observation: 検査時点の実画面。判断材料が古い画面を指していないか照合する
    #     evaluator_allowed_action_ids: 候補評価器が今回の画面で実行可能とした操作ID一覧
    #   ]
    #   戻り値: [
    #     ReliabilityResult: 候補を通す、注意して扱う、追加確認する、拒否するの総合判定と全根拠
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
            #     add_evidence: 今行った照合の結論と比較情報を、共通形式の根拠記録へ追加する
            #   ]
            #   処理: [
            #     1: 判定規則の出所を固定し、検査名・深刻度・結論・比較情報を1件にまとめる
            #     2: 総合状態の計算に使う検査一覧へ追加する
            #   ]
            #   引数: [
            #     check: 今回確かめた条件を識別する名前
            #     severity: 検査結果の扱い。問題なし、注意、再確認、拒否を区別する
            #     message: 何を比べ、どの条件に合ったかを人が読める形で説明する文
            #     details: 期待値と受信値など、結論を後から確かめるための情報。不要なら省略する
            #   ]
            #   戻り値: [なし。1件の検査根拠をevidence一覧へ追加する]
            # }
            # 出所を統一すると、別の検査器の結果と区別したまま根拠を集約できる。
            evidence.append(
                ReliabilityEvidence(
                    check,
                    severity,
                    DETERMINISTIC_EVIDENCE_CONFIDENCE,
                    message,
                    details=details or {},
                )
            )

        # 回答の型や必須項目が崩れている場合は、候補との意味照合へ進まず拒否する。
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

        # 判断元へ渡した候補一覧が検査時点の画面に属するか、回答内容を調べる前に確かめる。
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

        # 選択IDを一意な画面候補へ結び付け、判断材料と候補評価器の両方が許可したことを要求する。
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

        # 回答が返した参照IDを判断時の値と照合し、古い画面への回答を現在の選択として扱わない。
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

        # 選択理由の矛盾は、選ばれた候補を明示的に避ける文だけを対象にする。単なる不確かな表現は拒否理由にしない。
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
    #     normalize_decision: 形式検査を通過した回答を、実行処理が使うActionDecisionへ変換する
    #   ]
    #   処理: [
    #     1: 回答の型、必須の操作ID・理由・判断元を取り出して検査する
    #     2: 画面参照を含む有効な値だけを実行判断へ移し、不正な回答はNoneにする
    #   ]
    #   引数: [
    #     decision_output: 判断元が返した未検証の値。呼び出し側で形式検査済みとは限らない
    #   ]
    #   戻り値: [
    #     ActionDecision | None: 必須値が有効なら実行判断、不正または不足があればNone
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
    #     _index_candidates: 候補IDの重複や許可状態の食い違いを記録し、選択IDから画面候補を検索できるようにする
    #   ]
    #   処理: [
    #     1: 候補一覧をIDから候補への辞書にし、重複IDを信頼性違反として記録する
    #     2: 各候補の許可フラグと許可ID一覧が一致するか調べる
    #     3: 許可一覧にだけ存在し候補が欠けているIDを記録する
    #   ]
    #   引数: [
    #     context: 判断元に提示した候補と許可IDを含む判断材料
    #     add_evidence: 不整合を総合結果の根拠一覧へ記録する関数
    #   ]
    #   戻り値: [
    #     dict[str, CandidateDecisionContext]: 操作IDで候補を一意に検索する辞書。重複があっても別途拒否根拠へ残す
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
    #     _build_result: 各検査の深刻度を優先順位に従って集約し、候補選択の総合判定を作る
    #   ]
    #   処理: [
    #     1: 拒否根拠があればREJECT、なければVERIFY（追加確認）、CAUTION（注意）の順で扱いを決める
    #     2: 問題がなければTRUSTとし、画面ID・候補ID・全根拠を結果へ関連付ける
    #   ]
    #   引数: [
    #     context: この判断が基づく画面観測と候補を含む判断材料
    #     action_id: 回答が選び、または拒否対象として記録する操作ID
    #     provider: 回答元のモデルまたは判断処理の識別名
    #     evidence: 今回の照合で得た根拠の一覧
    #   ]
    #   戻り値: [
    #     ReliabilityResult: 次の処理が候補をどう扱うかと、その理由を含む検査結果
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
#     _reason_contradicts_action: 判断元の理由が、選択された候補そのものを避けるよう明示しているか調べる
#   ]
#   処理: [
#     1: 理由文と候補ID・表示名の大小文字や区切りを揃え、英語・日本語の否定表現を探す
#     2: 選択候補名が別の長い候補名の先頭にも使われている場合、文が指す対象全体を見分ける
#     3: 選択候補を避ける明示的な文だけを矛盾とし、別候補への否定を誤検出しない
#   ]
#   引数: [
#     reason: 判断元が返した、操作を選んだ理由の文章
#     candidate: 実際に選択された許可候補。理由文がこの候補を否定していないか調べる
#     candidates: 同じ画面に表示された候補一覧。似た名前の別候補との取り違えを防ぐ
#   ]
#   戻り値: [
#     bool: 対象候補を避ける明示的な表現があればTrue。一般的な言い換え全てを理解する判定ではない
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
#     _matches_longer_candidate_phrase: 選択候補名から始まる長い英語候補名を文が指す場合、短い名前だけの一致を除外する
#   ]
#   処理: [
#     1: 現在の候補一覧から、選択候補名に語を足した候補名を探す
#     2: 理由文の照合位置がその候補名全体と一致するか確かめる
#     3: 長い候補名を指している場合Trueを返し、短い候補への否定と誤認しない
#   ]
#   引数: [
#     reason: 空白や区切りを整えた判断理由。英語の大小文字は境界判定に残す
#     candidate_term_start: 判断理由内で候補名と照合を始める文字位置
#     selected_term: 選択された候補IDまたは表示名の正規化形
#     candidate_terms: 同じ画面の候補ID・表示名の正規化形
#   ]
#   戻り値: [
#     bool: 選択候補名を含む長い候補名全体に一致した場合True
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
#     _matches_longer_japanese_candidate: 選択候補名と同じ書き出しを持つ日本語候補を、助詞境界まで照合する
#   ]
#   処理: [
#     1: 現在の候補から、選択候補名より長く同じ文字列で始まる候補を探す
#     2: 理由文がその候補名全体に続き、直後が助詞または文末であることを確認する
#     3: 長い別候補を指すならTrueを返し、短い候補への否定と誤認しない
#   ]
#   引数: [
#     reason: 英字の大小や区切りを整えた判断理由
#     candidate_term_start: 理由文内で候補名と照合を始める文字位置
#     selected_term: 選択された候補IDまたは表示名の正規化形
#     candidate_terms: 同じ画面の候補ID・表示名の正規化形
#   ]
#   戻り値: [
#     bool: 助詞境界まで含めて長い別候補名を指している場合True
#   ]
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
#     _mentions_longer_candidate_before_japanese_negation: 否定語の直前に別の長い候補名がある場合、その否定を選択候補の拒否と誤認しない
#   ]
#   処理: [
#     1: 否定語の直前に定数で定めた範囲を取り、同じ書き出しの長い候補名を探す
#     2: 長い候補名の後に日本語の助詞が続き、その後に否定語があるか照合する
#     3: 否定語が長い別候補へ掛かる場合Trueを返す
#   ]
#   引数: [
#     reason: 英字の大小や区切りを整えた判断理由
#     directive_start: 理由文内で否定・回避表現が始まる文字位置
#     selected_term: 選択された候補IDまたは表示名の正規化形
#     candidate_terms: 同じ画面の候補ID・表示名の正規化形
#   ]
#   戻り値: [
#     bool: 否定語が選択候補ではなく、名前の似た別候補に掛かる場合True
#   ]
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
#     _normalize_contradiction_text: 候補名と理由文の表記差を減らし、否定表現を同じ規則で照合できるようにする
#   ]
#   処理: [
#     1: 英字を大小文字の差がない比較形へ変換する
#     2: ハイフンとアンダースコアを空白へ置き換える
#     3: 連続空白や前後の空白を取り除き、単語境界を揃える
#   ]
#   引数: [
#     value: 判断元の理由文、または候補のID・表示名
#   ]
#   戻り値: [
#     str: 候補名の比較に使う正規化済み文字列。元の文章は変更しない
#   ]
# }
def _normalize_contradiction_text(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())
