import json
from pathlib import Path
from ai_game_player.models import ActionCandidate, ScreenObservation

class ConfiguredRegionDetector:
    """Turns named automation rectangles into candidates before safety evaluation."""
    def __init__(self, config_path: Path) -> None: self.config_path=config_path
    def detect(self, observation: ScreenObservation) -> list[ActionCandidate]:
        value=json.loads(self.config_path.read_text(encoding="utf-8"))
        regions=value.get("regions",[]) if isinstance(value,dict) else None
        if not isinstance(regions,list): raise ValueError("automation.json regions must be an array")
        result=[]
        for region in regions:
            if not isinstance(region,dict): continue
            x=int(region["x"]); y=int(region["y"]); width=int(region["width"]); height=int(region["height"])
            if width<=0 or height<=0: continue
            result.append(ActionCandidate(str(region["action_id"]),str(region.get("kind","click")),str(region.get("label",region["action_id"])),x+width//2,y+height//2,float(region.get("confidence",1.0)),bool(region.get("dangerous",False))))
        return result