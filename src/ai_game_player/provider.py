import json
from urllib.request import Request, urlopen
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
class RuleProvider:
    def choose(self,candidates:list[ActionCandidate],observation:ScreenObservation|None=None,purpose:str="",personality:str="")->ActionDecision:
        if not candidates: raise ValueError("許可された操作候補がありません")
        c=max(candidates,key=lambda x:x.confidence); return ActionDecision(c.action_id,"信頼度が最も高い安全な候補","local_rule")
class OllamaProvider:
    @staticmethod
    def list_models(endpoint: str = "http://127.0.0.1:11434", timeout: int = 5) -> list[str]:
        request = Request(endpoint.rstrip("/") + "/api/tags", method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = payload.get("models", [])
            if not isinstance(models, list): raise ValueError("Ollama models must be an array")
            return [str(item["name"]) for item in models if isinstance(item, dict) and item.get("name")]
        except Exception as exc:
            raise RuntimeError(f"Ollamaモデル一覧を取得できません: {exc}") from exc

    def __init__(self,model:str,endpoint:str="http://127.0.0.1:11434",timeout:int=120): self.model=model; self.endpoint=endpoint.rstrip('/'); self.timeout=timeout
    def choose(self,candidates:list[ActionCandidate],observation:ScreenObservation|None=None,purpose:str="",personality:str="")->ActionDecision:
        if not candidates: raise ValueError("許可された操作候補がありません")
        context={"instruction":"許可候補から1つ選び、action_idとreasonをJSONで返す","personality":personality,"purpose":purpose,"observation":observation.to_dict() if observation else {},"allowed_actions":[c.to_dict() for c in candidates]}
        payload={"model":self.model,"stream":False,"format":"json","prompt":json.dumps(context,ensure_ascii=False)}
        request=Request(self.endpoint+"/api/generate",data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers={"Content-Type":"application/json"},method='POST')
        try:
            with urlopen(request,timeout=self.timeout) as response: parsed=json.loads(json.loads(response.read().decode('utf-8'))['response'])
            action_id=str(parsed['action_id'])
        except Exception as exc: raise RuntimeError(f"Ollama応答を解釈できません: {exc}") from exc
        if action_id not in {c.action_id for c in candidates}: raise ValueError("Ollamaが許可候補外の操作を選択しました")
        return ActionDecision(action_id,str(parsed.get('reason','')),f"ollama:{self.model}")