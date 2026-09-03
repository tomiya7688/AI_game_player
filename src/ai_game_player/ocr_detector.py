from typing import Any
from ai_game_player.models import ActionCandidate, ScreenObservation

class OcrTextCandidateDetector:
    def detect(self, observation: ScreenObservation, texts: list[dict[str, Any]]) -> list[ActionCandidate]:
        result=[]
        for index,item in enumerate(texts):
            text=str(item.get("text","")).strip()
            if not text: continue
            x=int(item.get("x",0)); y=int(item.get("y",0)); width=int(item.get("width",0)); height=int(item.get("height",0))
            if width<=0 or height<=0: continue
            result.append(ActionCandidate(str(item.get("action_id",f"ocr-{index}")),str(item.get("kind","click")),text,x+width//2,y+height//2,float(item.get("confidence",.6)),bool(item.get("dangerous",False))))
        return result