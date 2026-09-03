from hashlib import sha256
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame

class FrameAnalyzer:
    """Converts a raw frame into stable, dependency-free visual features."""
    def analyze(self, frame: ScreenFrame, screen_id: str = "screen") -> ScreenObservation:
        if len(frame.bgra) != frame.width * frame.height * 4: raise ValueError("BGRA buffer size does not match frame dimensions")
        pixels=frame.width*frame.height
        blue=sum(frame.bgra[0::4]); green=sum(frame.bgra[1::4]); red=sum(frame.bgra[2::4])
        scale=255*pixels
        features={"mean_rgb":{"r":round(red/pixels),"g":round(green/pixels),"b":round(blue/pixels)},"mean_brightness":round((red+green+blue)/(3*pixels)),"signature":sha256(frame.bgra).hexdigest()}
        return ScreenObservation(screen_id,frame.width,frame.height,[],features)