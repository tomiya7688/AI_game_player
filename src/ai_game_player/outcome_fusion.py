from __future__ import annotations

from typing import Protocol

from ai_game_player.models import ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_detectors import (
    ScreenDiffDetector,
    StateDeltaDetector,
    TemporalChangeDetector,
    TerminalTextDetector,
)
from ai_game_player.outcome_models import OutcomeEvent, OutcomeEvidence


class SemanticOutcomeProvider(Protocol):
    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None = None,
    ) -> OutcomeAssessment:
        ...


class DeterministicOutcomeFusion:
    # {
    #   責務: [
    #     DeterministicOutcomeFusion: 画面解析器と任意の意味評価器が返した根拠を統合し、成功・失敗・状態変化を不確実性とともに判定する
    #   ]
    #   処理: [
    #     1: 終了表示などの終端根拠を先に確認し、成功と失敗の競合はunknownとして残す
    #     2: 終端根拠がなければ意味評価の結果を使い、それも弱い場合は変化・安定の観測を統合する
    #     3: 状態遷移とゲームの成功・失敗を別項目で保持し、根拠が割れた場合は断定しない
    #   ]
    # }

    # {
    #   責務: [
    #     fuse: 操作後の成功・失敗根拠を優先して判定し、状態変化の判定は別項目に保持する
    #   ]
    #   処理: [
    #     1: 成功と失敗の根拠が競合した場合はunknownにし、競合を記録する
    #     2: 終端根拠がなければ意味評価の結果を見て、確信が弱い場合は状態差分を統合する
    #     3: どの結果を返す場合も、状態差分から得たstate_changedを失わない
    #   ]
    #   引数: [
    #     action_id: 判定対象の操作候補を履歴へ結び付けるID
    #     evidence: 画面やゲーム結果の検出器が返した、重みと判定元を持つ根拠
    #     semantic_fallback_used: 意味評価器の結果を根拠に含めた場合True
    #   ]
    #   戻り値: [
    #     OutcomeEvent: ゲーム結果、状態変化、確信度、競合、保留状態をまとめた判定
    #   ]
    # }
    def fuse(
        self,
        action_id: str,
        evidence: tuple[OutcomeEvidence, ...],
        *,
        semantic_fallback_used: bool = False,
    ) -> OutcomeEvent:
        terminal_success = self._support(evidence, "terminal", "success")
        terminal_failure = self._support(evidence, "terminal", "failure")
        semantic_success = self._support(evidence, "semantic", "success")
        semantic_failure = self._support(evidence, "semantic", "failure")
        # 終了や失敗が確定しても、盤面が変わったかは別の評価値として記録する。
        state_transition = self._fuse_state_evidence(action_id, evidence, semantic_fallback_used)

        if terminal_success > 0.0 and terminal_failure > 0.0:
            return self._event(
                action_id,
                "unknown",
                0.2,
                True,
                True,
                evidence,
                "conflicting terminal evidence",
                semantic_fallback_used,
                state_changed=state_transition.state_changed,
            )
        if terminal_success > 0.0:
            return self._event(
                action_id,
                "success",
                self._bounded_support(terminal_success),
                False,
                False,
                evidence,
                "terminal success evidence",
                semantic_fallback_used,
                state_changed=state_transition.state_changed,
            )
        if terminal_failure > 0.0:
            return self._event(
                action_id,
                "failure",
                self._bounded_support(terminal_failure),
                False,
                False,
                evidence,
                "terminal failure evidence",
                semantic_fallback_used,
                state_changed=state_transition.state_changed,
            )

        if semantic_success > 0.0 and semantic_failure > 0.0:
            return self._event(
                action_id,
                "unknown",
                0.25,
                True,
                True,
                evidence,
                "conflicting semantic outcome evidence",
                semantic_fallback_used,
                state_changed=state_transition.state_changed,
            )
        if semantic_success >= 0.45:
            return self._event(
                action_id,
                "success",
                self._bounded_support(semantic_success),
                False,
                False,
                evidence,
                "semantic fallback reported success",
                semantic_fallback_used,
                state_changed=state_transition.state_changed,
            )
        if semantic_failure >= 0.45:
            return self._event(
                action_id,
                "failure",
                self._bounded_support(semantic_failure),
                False,
                False,
                evidence,
                "semantic fallback reported failure",
                semantic_fallback_used,
                state_changed=state_transition.state_changed,
            )
        return state_transition

    # {
    #   責務: [
    #     _fuse_state_evidence: 画面・構造化状態・継続観測の根拠を比べ、操作後に状態が変わったかを独立して判定する
    #   ]
    #   処理: [
    #     1: 構造化状態と時間を置いた再観測を重く、単発の画面差分を半分の重みで変化・安定それぞれに加算する
    #     2: 変化と安定の両方に十分な根拠がある場合は、根拠の比率から競合と確信度を求める
    #     3: 強い状態差分、持続する画面差分、複数検出器の一致を順に見て、根拠不足ならunknownで保留する
    #   ]
    #   引数: [
    #     action_id: この状態変化を結び付ける、直前に実行した操作候補のID
    #     evidence: 状態差分・画面差分・再観測の各検出器が返した重み付き根拠
    #     semantic_fallback_used: 弱い観測を補うため、意味評価器の結果も根拠一覧に含めた場合True
    #   ]
    #   戻り値: [
    #     OutcomeEvent: changed・unchanged・unknownの状態遷移、確信度、競合、判定を保留したかを含む結果
    #   ]
    # }
    def _fuse_state_evidence(
        self,
        action_id: str,
        evidence: tuple[OutcomeEvidence, ...],
        semantic_fallback_used: bool,
    ) -> OutcomeEvent:
        state_changed = self._support(evidence, "state_delta", "changed")
        state_stable = self._support(evidence, "state_delta", "stable")
        screen_changed = self._support(evidence, "screen_diff", "changed")
        screen_stable = self._support(evidence, "screen_diff", "stable")
        temporal_persistent = self._support(evidence, "temporal_change", "persistent")
        temporal_transient = self._support(evidence, "temporal_change", "transient")

        changed_support = state_changed + 0.5 * screen_changed + temporal_persistent
        stable_support = state_stable + 0.5 * screen_stable + temporal_transient
        conflict = (
            changed_support >= 0.45
            and stable_support >= 0.45
            and min(changed_support, stable_support) / max(changed_support, stable_support) >= 0.55
        )

        if state_changed >= 0.65:
            confidence = self._relative_confidence(changed_support, stable_support)
            return self._event(
                action_id,
                "changed",
                confidence,
                conflict,
                conflict and confidence < 0.6,
                evidence,
                "structured state changed",
                semantic_fallback_used,
            )
        if temporal_persistent >= 0.5 and screen_changed > 0.0:
            confidence = self._relative_confidence(changed_support, stable_support)
            return self._event(
                action_id,
                "changed",
                confidence,
                conflict,
                conflict and confidence < 0.6,
                evidence,
                "visual change persisted across follow-up observations",
                semantic_fallback_used,
            )
        if state_stable >= 0.6 and (screen_stable > 0.0 or temporal_transient >= 0.5):
            confidence = self._relative_confidence(stable_support, changed_support)
            return self._event(
                action_id,
                "unchanged",
                confidence,
                conflict,
                conflict and confidence < 0.6,
                evidence,
                "structured state remained stable",
                semantic_fallback_used,
            )
        if changed_support >= 0.65 and changed_support - stable_support >= 0.25:
            return self._event(
                action_id,
                "changed",
                self._relative_confidence(changed_support, stable_support),
                conflict,
                conflict,
                evidence,
                "multiple change detectors agree",
                semantic_fallback_used,
            )
        if stable_support >= 0.75 and stable_support - changed_support >= 0.25:
            return self._event(
                action_id,
                "unchanged",
                self._relative_confidence(stable_support, changed_support),
                conflict,
                conflict,
                evidence,
                "multiple detectors report stable state",
                semantic_fallback_used,
            )
        return self._event(
            action_id,
            "unknown",
            min(0.49, abs(changed_support - stable_support)),
            conflict,
            True,
            evidence,
            "insufficient or conflicting outcome evidence",
            semantic_fallback_used,
        )

    @staticmethod
    def _support(evidence: tuple[OutcomeEvidence, ...], signal: str, value: str) -> float:
        return sum(item.weight for item in evidence if item.signal == signal and item.value == value)

    @staticmethod
    def _bounded_support(value: float) -> float:
        return round(max(0.0, min(1.0, value)), 6)

    @staticmethod
    def _relative_confidence(primary: float, opposing: float) -> float:
        total = primary + opposing
        if total <= 0.0:
            return 0.0
        return round(max(0.0, min(1.0, primary / total)), 6)

    # {
    #   責務: [
    #     _event: 複数の検出器が出した判定内容を、確信度の範囲を整えたOutcomeEventへまとめる
    #   ]
    #   処理: [
    #     1: 確信度を0から1の範囲に収めてから結果を作る
    #     2: state_changedが指定されていれば終端結果と分けて保持し、省略時だけ状態遷移から導く
    #   ]
    #   引数: [
    #     action_id: 結果を関連付ける操作候補のID
    #     status: 成功・失敗・変化・不変・不明の総合判定
    #     confidence: 総合判定の確信度
    #     conflict: 根拠の間に矛盾がある場合True
    #     abstained: 断定せず結果を保留した場合True
    #     evidence: 判定の根拠となった検出結果
    #     reason: この結果を選んだ理由
    #     semantic_fallback_used: 意味評価器の結果を利用した場合True
    #     state_changed: 画面状態の変化判定。省略時はstatusとabstainedから導く
    #   ]
    #   戻り値: [
    #     OutcomeEvent: 引数の判定と根拠を保持する不変の結果
    #   ]
    # }
    @staticmethod
    def _event(
        action_id: str,
        status: str,
        confidence: float,
        conflict: bool,
        abstained: bool,
        evidence: tuple[OutcomeEvidence, ...],
        reason: str,
        semantic_fallback_used: bool,
        *,
        state_changed: bool | None = None,
    ) -> OutcomeEvent:
        return OutcomeEvent(
            action_id,
            status,
            round(max(0.0, min(1.0, confidence)), 6),
            conflict,
            abstained,
            evidence,
            reason,
            semantic_fallback_used,
            status == "changed" and not abstained if state_changed is None else state_changed,
        )


class OutcomeDetector:
    """Lightweight Before + Action + After outcome pipeline with optional semantic fallback."""

    def __init__(
        self,
        semantic_provider: SemanticOutcomeProvider | None = None,
        semantic_threshold: float = 0.55,
        fusion: DeterministicOutcomeFusion | None = None,
    ) -> None:
        if not 0.0 <= semantic_threshold <= 1.0:
            raise ValueError("semantic threshold must be between 0 and 1")
        self.semantic_provider = semantic_provider
        self.semantic_threshold = semantic_threshold
        self.terminal = TerminalTextDetector()
        self.state_delta = StateDeltaDetector()
        self.screen_diff = ScreenDiffDetector()
        self.temporal = TemporalChangeDetector()
        self.fusion = fusion or DeterministicOutcomeFusion()

    def detect(
        self,
        before: ScreenObservation,
        action_id: str,
        after: ScreenObservation,
        temporal_observations: tuple[ScreenObservation, ...] = (),
    ) -> OutcomeEvent:
        evidence = list(self.terminal.detect(after))
        evidence.append(self.state_delta.detect(before, after))
        evidence.append(self.screen_diff.detect(before, after))
        evidence.append(self.temporal.detect(before, after, temporal_observations))
        event = self.fusion.fuse(action_id, tuple(evidence))
        if self.semantic_provider is None:
            return event
        if not event.abstained and event.confidence >= self.semantic_threshold:
            return event

        assessment = self.semantic_provider.assess_outcome(after, before)
        evidence.append(
            OutcomeEvidence(
                "semantic_outcome",
                "semantic",
                assessment.status,
                assessment.confidence,
                0.7,
                f"{type(self.semantic_provider).__name__}/semantic_outcome",
                {"reason": assessment.reason},
            )
        )
        return self.fusion.fuse(action_id, tuple(evidence), semantic_fallback_used=True)
