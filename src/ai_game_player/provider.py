import json
from urllib.request import Request, urlopen

from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation


class RuleProvider:
    def assess_outcome(self, observation: ScreenObservation, previous: ScreenObservation | None = None):
        from ai_game_player.outcome import OutcomeEvaluator

        return OutcomeEvaluator().assess(observation)

    def choose(self, candidates: list[ActionCandidate], observation: ScreenObservation | None = None, purpose: str = "", personality: str = "") -> ActionDecision:
        if not candidates:
            raise ValueError("許可された操作候補がありません")
        candidate = max(candidates, key=lambda value: value.confidence)
        return ActionDecision(candidate.action_id, "信頼度が最も高い安全な候補", "local_rule")

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
        return ActionDecision(candidate.action_id, reason, "local_rule_context")


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
        return self._request_decision(context, {candidate.action_id for candidate in candidates})

    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        prompt_context = {
            "instruction": "Decision Contextを根拠にallowed_action_idsから1つ選び、action_idとreasonをJSONで返す",
            "personality": personality,
            "decision_context": context.to_dict(),
        }
        return self._request_decision(prompt_context, set(context.allowed_action_ids))

    def _request_decision(self, context: dict[str, object], allowed_action_ids: set[str]) -> ActionDecision:
        if not allowed_action_ids:
            raise ValueError("許可された操作候補がありません")
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
            action_id = str(parsed["action_id"])
        except Exception as exc:
            raise RuntimeError(f"Ollama応答を解釈できません: {exc}") from exc
        if action_id not in allowed_action_ids:
            raise ValueError("Ollamaが許可候補外の操作を選択しました")
        return ActionDecision(action_id, str(parsed.get("reason", "")), f"ollama:{self.model}")
