import json
from dataclasses import replace
from urllib.request import Request, urlopen

from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation


# {
#   責務: [
#     RuleProvider: 外部通信なしで許可候補を規則に従って選び、必要なら画面参照を添えて返す
#   ]
#   処理: [
#     1: 旧形式では認識確信度が最も高い候補を選ぶ
#     2: 判断材料を受け取る形式では、目的寄与・予測確信度・認識確信度の順で候補を比較する
#     3: 判断材料を受け取る形式では、回答を元の画面と状態へ結び付けるIDも返す
#     4: ゲーム画面の成功・失敗・進行状態は別の規則評価器へ委ねる
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
    #     choose_context: 許可候補の目的寄与を比べ、選択理由と判断時の画面参照を付けて返す
    #   ]
    #   処理: [
    #     1: 判断材料に含まれる許可候補だけを選択対象にする
    #     2: 目的寄与、寄与予測の確信度、画面認識の確信度、候補IDの順に比較する
    #     3: 選択理由と画面観測ID・画面ID・状態照合値を返し、後段の検証に使えるようにする
    #   ]
    #   引数: [
    #     context: 現在画面、候補ごとの許可状態、目的寄与、過去の結果を含む判断材料
    #     personality: 共通の判断元APIとの互換用。現在の規則選択では参照しない
    #   ]
    #   戻り値: [
    #     ActionDecision: 選択候補・比較値を含む理由・画面観測ID・画面ID・状態照合値
    #   ]
    #   エラー: [
    #     ValueError: 判断材料に実行許可された候補が1件もない場合
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
#     OllamaProvider: ローカルOllamaサーバーへ画面判断や結果評価を要求し、検証前の応答を呼び出し元へ返す
#   ]
#   フィールド: [
#     model: Ollamaへ指定するモデル名。どのモデルの回答か記録するためにも使う
#     endpoint: OllamaサーバーのHTTP接続先。末尾のスラッシュは初期化時に取り除く
#     timeout: HTTP応答を待つ秒数。この時間を超えた通信は失敗として扱う
#   ]
#   処理: [
#     1: 画面・目標・許可候補をJSONにしてOllamaの生成APIへ送る
#     2: 判断材料形式では、提示された画面と状態を回答へそのまま含めるよう要求する
#     3: 回答は検証前の値として保持し、形式・画面参照・候補の検査はDecisionVerifierへ委ねる
#     4: 低確信の操作結果評価にも同じサーバーを使い、その評価値はOutcomeDetectorが他の根拠と統合する
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
    #     choose: 従来形式の候補一覧をOllamaへ提示し、一覧内から選んだ回答を受け取る
    #   ]
    #   処理: [
    #     1: 選択肢が空なら要求を送らずに失敗させる
    #     2: 画面、達成目的、応答方針、許可候補をOllama要求へ含める
    #     3: 返答の候補IDが一覧内にあることと、理由が文字列であることを確かめる
    #     4: 理由が未指定なら空文字列として扱い、より厳密な形式検査は後段へ残す
    #   ]
    #   引数: [
    #     candidates: Ollamaに提示する実行許可済みの操作候補一覧
    #     observation: 候補を検出した画面。未指定なら画面情報なしで要求する
    #     purpose: Ollamaに伝える今回のゲーム内達成目的
    #     personality: Ollamaに伝える任意の応答方針
    #   ]
    #   戻り値: [
    #     ActionDecision: 選択候補と理由を含む応答。旧形式のため画面参照は別処理で補う
    #   ]
    #   エラー: [
    #     ValueError: 候補が空、応答IDが許可一覧にない、または判断理由が文字列でない場合
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
    #     choose_context: 現在の判断材料をOllamaへ渡し、画面参照を含む未検証の回答を受け取る
    #   ]
    #   処理: [
    #     1: 許可候補が空ならモデルへ要求せず失敗させる
    #     2: 候補・現在状態・目標と、回答へ含めるべき参照値を要求へまとめる
    #     3: Ollamaの出力を許可済みだとはみなさず、形式検査前の判断値として返す
    #   ]
    #   引数: [
    #     context: 現在画面、許可候補、目標、過去の操作結果を含む判断材料
    #     personality: Ollamaに伝える任意の応答方針
    #   ]
    #   戻り値: [
    #     ActionDecision: 候補選択と画面参照を含む未検証の回答。実行前に別の検証器へ渡す
    #   ]
    #   エラー: [
    #     ValueError: 判断材料に許可候補が1件もない場合
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
    #     _request_decision: 判断用JSONをOllamaへ送り、形式不備も含めた未信頼の回答を後段へ渡す
    #   ]
    #   処理: [
    #     1: 指定モデル名と判断材料を含むJSON要求を作る
    #     2: HTTP応答を読み、外側と内側のJSON構造を解析する
    #     3: 正常な文字列へ無理に変換せず、元の値と形式不備をActionDecisionへ保持する
    #   ]
    #   引数: [
    #     context: 画面・候補・目的を含む、Ollamaの判断要求に含める辞書
    #   ]
    #   戻り値: [
    #     ActionDecision: パーサーが読み取った回答。壊れたJSONはvalidation_error付きで返す
    #   ]
    #   エラー: [
    #     RuntimeError: サーバーへの接続または応答受信に失敗した場合。JSON形式不備は検証器へ渡す
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

        # JSON構文や必須のresponse項目が壊れていても、通信障害と区別できるよう検証器向けの失敗値にする。
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

        # 数値などの不正型を文字列へ変えると誤って有効扱いされるため、値をそのまま検証器へ渡す。
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
