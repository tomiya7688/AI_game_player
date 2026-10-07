import json
from dataclasses import replace
from urllib.request import Request, urlopen

from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation


# {
#   責務: [
#     RuleProvider: networkを使わず候補評価値に基づく決定論的判断を返す
#   ]
#   処理: [
#     1: Action SafetyとOutcomeの基準処理を提供する
#     2: 許可候補から判断しDecision Context参照を付ける
#   ]
# }
class RuleProvider:
    def assess_outcome(self, observation: ScreenObservation, previous: ScreenObservation | None = None):
        from ai_game_player.outcome import OutcomeEvaluator

        return OutcomeEvaluator().assess(observation)

    def choose(self, candidates: list[ActionCandidate], observation: ScreenObservation | None = None, purpose: str = "", personality: str = "") -> ActionDecision:
        if not candidates:
            raise ValueError("許可された操作候補がありません")
        candidate = max(candidates, key=lambda value: value.confidence)
        return ActionDecision(candidate.action_id, "信頼度が最も高い安全な候補", "local_rule")

    # {
    #   責務: [
    #     choose_context: 許可候補の評価値を比較しcontext参照付きで選択する
    #   ]
    #   処理: [
    #     1: 許可された候補だけを抽出する
    #     2: utility・confidence・認識確信度で候補を比較する
    #     3: snapshot・画面・状態参照を添えて判断を返す
    #   ]
    #   引数: [
    #     context: 候補・状態・evidenceを保持するDecision Context
    #     personality: Provider向けの任意の振る舞い指定
    #   ]
    #   戻り値: [
    #     ActionDecision: 選択候補と判断時のcontext参照
    #   ]
    #   エラー: [
    #     ValueError: 許可候補が存在しない
    #   ]
    # }
    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        allowed = [candidate for candidate in context.candidates if candidate.allowed]
        if not allowed:
            raise ValueError("許可された操作候補がありません")
        candidate = max(
            allowed,
            key=lambda value: (value.utility_score, value.utility_confidence, value.recognition_confidence, value.action_id),
        )
        reason = f"utility={candidate.utility_score:.3f}, confidence={candidate.utility_confidence:.3f}"
        if "repetition" in {entry.evaluator for entry in candidate.evaluations}:
            reason += "; repetition-aware"
        return ActionDecision(
            candidate.action_id,
            reason,
            "local_rule_context",
            context.snapshot_id,
            str(context.state.get("screen_id", "")),
            str(context.state.get("signature", "")),
        )


# {
#   責務: [
#     OllamaProvider: OllamaへDecision・Outcome要求を送り構造化応答を受け取る
#   ]
#   フィールド: [
#     model: 呼び出すOllama model名
#     endpoint: Ollama HTTP endpoint
#     timeout: 要求ごとの応答timeout
#   ]
#   処理: [
#     1: LLM向けにDecision ContextをJSON化する
#     2: Decisionにsnapshot・scene参照を含めるよう要求する
#     3: schema検証はDecisionVerifierへ委ねる
#   ]
# }
class OllamaProvider:
    @staticmethod
    def list_models(endpoint: str = "http://127.0.0.1:11434", timeout: int = 5) -> list[str]:
        request = Request(endpoint.rstrip("/") + "/api/tags", method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = payload.get("models", [])
            if not isinstance(models, list):
                raise ValueError("Ollama models must be an array")
            return [str(item["name"]) for item in models if isinstance(item, dict) and item.get("name")]
        except Exception as exc:
            raise RuntimeError(f"Ollamaモデル一覧を取得できません: {exc}") from exc

    def __init__(self, model: str, endpoint: str = "http://127.0.0.1:11434", timeout: int = 120):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def assess_outcome(self, observation: ScreenObservation, previous: ScreenObservation | None = None):
        from ai_game_player.outcome import OutcomeAssessment

        context = {
            "instruction": "画面状態を評価し、status(confidence,reason)をJSONで返す",
            "observation": observation.to_dict(),
            "previous": previous.to_dict() if previous else {},
        }
        payload = {"model": self.model, "stream": False, "format": "json", "prompt": json.dumps(context, ensure_ascii=False)}
        request = Request(
            self.endpoint + "/api/generate",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                parsed = json.loads(json.loads(response.read().decode("utf-8"))["response"])
            status = str(parsed["status"]).lower()
            if status not in {"success", "failure", "ongoing", "unknown"}:
                raise ValueError("invalid outcome status")
            confidence = float(parsed.get("confidence", 0.0))
            if not 0 <= confidence <= 1:
                raise ValueError("invalid outcome confidence")
            return OutcomeAssessment(status, confidence, str(parsed.get("reason", "")))
        except Exception as exc:
            raise RuntimeError(f"Ollama状態評価を解釈できません: {exc}") from exc

    # {
    #   責務: [
    #     choose: 許可候補をLLMへ提示して1つの判断を受け取る
    #   ]
    #   処理: [
    #     1: 操作候補が存在することを確認する
    #     2: 画面・目的・候補をLLM向けpayloadへまとめる
    #     3: 構造化された判断出力を受け取る
    #     4: 候補IDと判断理由の型を確認し、未入力の理由を空文字列にする
    #   ]
    #   引数: [
    #     candidates: 判断対象となる許可候補
    #     observation: 候補に対応する観測画面
    #     purpose: 現在の目的
    #     personality: Provider向けの任意の振る舞い指定
    #   ]
    #   戻り値: [
    #     ActionDecision: LLMから受け取った候補判断
    #   ]
    #   エラー: [
    #     ValueError: 許可候補が空、応答IDが許可候補外、または判断理由が文字列以外
    #   ]
    # }
    def choose(self, candidates: list[ActionCandidate], observation: ScreenObservation | None = None, purpose: str = "", personality: str = "") -> ActionDecision:
        if not candidates:
            raise ValueError("許可された操作候補がありません")
        context = {
            "instruction": "許可候補から1つ選び、action_idとreasonをJSONで返す",
            "personality": personality,
            "purpose": purpose,
            "observation": observation.to_dict() if observation else {},
            "allowed_actions": [candidate.to_dict() for candidate in candidates],
        }
        decision = self._request_decision(context)
        allowed_action_ids = {candidate.action_id for candidate in candidates}
        if not isinstance(decision.action_id, str) or decision.action_id not in allowed_action_ids:
            raise ValueError("Ollamaが許可候補外の操作を選択しました")
        validation_error_codes = (decision.validation_error or "").split(";")
        if "invalid_decision_reason_type" in validation_error_codes:
            raise ValueError("Ollamaの判断理由が文字列ではありません")
        if decision.reason is None:
            decision = replace(decision, reason="")
        elif not isinstance(decision.reason, str):
            raise ValueError("Ollamaの判断理由が文字列ではありません")
        return decision

    # {
    #   責務: [
    #     choose_context: 検証可能なsnapshot・scene参照を含むContext判断を受け取る
    #   ]
    #   処理: [
    #     1: 空の許可候補一覧を拒否する
    #     2: contextと参照値をLLM向けpayloadへまとめる
    #     3: 未信頼の判断出力をActionDecisionへ格納する
    #   ]
    #   引数: [
    #     context: 候補・状態・snapshot参照を保持するDecision Context
    #     personality: Provider向けの任意の振る舞い指定
    #   ]
    #   戻り値: [
    #     ActionDecision: LLMから受け取った候補判断
    #   ]
    #   エラー: [
    #     ValueError: 許可候補が存在しない
    #   ]
    # }
    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        if not context.allowed_action_ids:
            raise ValueError("許可された操作候補がありません")
        prompt_context = {
            "instruction": "Decision Contextを根拠にallowed_action_idsから1つ選び、action_id、reason、snapshot_id、screen_id、state_signatureをJSONで返す。参照値は入力をそのままコピーする",
            "personality": personality,
            "decision_context": context.to_dict(),
        }
        return self._request_decision(prompt_context)

    # {
    #   責務: [
    #     _request_decision: Ollamaへ判断要求を送り未信頼の応答を保持する
    #   ]
    #   処理: [
    #     1: JSON形式の判断要求を作成する
    #     2: OllamaのHTTP応答を読み取る
    #     3: 応答schemaを保ったActionDecisionへ変換する
    #   ]
    #   引数: [
    #     context: LLMへ送る判断文脈
    #   ]
    #   戻り値: [
    #     ActionDecision: 検証器へ渡すLLM出力
    #   ]
    #   エラー: [
    #     RuntimeError: Ollamaへの通信に失敗
    #   ]
    # }
    def _request_decision(self, context: dict[str, object]) -> ActionDecision:
        payload = {"model": self.model, "stream": False, "format": "json", "prompt": json.dumps(context, ensure_ascii=False)}
        request = Request(
            self.endpoint + "/api/generate",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_body = response.read()
        except Exception as exc:
            raise RuntimeError(f"Ollamaへ判断を要求できません: {exc}") from exc

        # 構文・schema不備は出力検証器へ渡し、REJECT evidenceとして記録する。
        try:
            response_payload = json.loads(response_body.decode("utf-8"))
            if not isinstance(response_payload, dict) or not isinstance(response_payload.get("response"), str):
                raise ValueError("Ollama response must contain a string response field")
            parsed = json.loads(response_payload["response"])
            if not isinstance(parsed, dict):
                raise ValueError("Ollama decision response must be an object")
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            return ActionDecision(
                "",
                "",
                f"ollama:{self.model}",
                validation_error=f"invalid_decision_schema:{type(exc).__name__}",
            )

        # 型を文字列へ強制変換せず、検証器が元の不正型を検出できるようにする。
        allowed_response_fields = {"action_id", "reason", "snapshot_id", "screen_id", "state_signature"}
        unexpected_fields = set(parsed).difference(allowed_response_fields)
        validation_errors = []
        if unexpected_fields:
            validation_errors.append("unexpected_decision_fields:" + ",".join(sorted(str(name) for name in unexpected_fields)))
        if "reason" in parsed and not isinstance(parsed["reason"], str):
            validation_errors.append("invalid_decision_reason_type")
        validation_error = ";".join(validation_errors) or None
        return ActionDecision(
            parsed.get("action_id"),
            parsed.get("reason"),
            f"ollama:{self.model}",
            parsed.get("snapshot_id"),
            parsed.get("screen_id"),
            parsed.get("state_signature"),
            validation_error,
        )
