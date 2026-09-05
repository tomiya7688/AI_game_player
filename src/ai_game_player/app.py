import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.config import ConfigStore, AppConfig
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.metrics import MetricsCalculator
from ai_game_player.screen_capture import WindowsScreenCapture

class MemorySource:
    def __init__(self,observation:ScreenObservation,candidates:list[ActionCandidate]): self.observation=observation; self.candidates=candidates
    def read(self): return self.observation,self.candidates

class Application:
    def __init__(self,root:tk.Tk)->None:
        config=ConfigStore(Path("data/config.json")).load(); self.root=root; root.title("AI Game Player - Decision Sandbox"); root.geometry("900x650")
        frame=ttk.Frame(root,padding=10); frame.pack(fill=tk.BOTH,expand=True)
        self.provider=tk.StringVar(value=config.provider); self.model=tk.StringVar(value=config.model); self.endpoint=tk.StringVar(value=config.endpoint); self.personality=tk.StringVar(value=config.personality); self.purpose=tk.StringVar(value=config.purpose); self.config_store=ConfigStore(Path("data/config.json"))
        settings=ttk.Frame(frame); settings.pack(fill=tk.X); ttk.Label(settings,text="Provider").pack(side=tk.LEFT); ttk.Combobox(settings,textvariable=self.provider,values=("ローカル規則","Ollama"),state="readonly",width=12).pack(side=tk.LEFT,padx=5); ttk.Label(settings,text="モデル").pack(side=tk.LEFT); ttk.Entry(settings,textvariable=self.model,width=16).pack(side=tk.LEFT,padx=5); ttk.Label(settings,text="Endpoint").pack(side=tk.LEFT); ttk.Entry(settings,textvariable=self.endpoint,width=28).pack(side=tk.LEFT,padx=5)
        prompt_settings=ttk.Frame(frame); prompt_settings.pack(fill=tk.X,pady=(6,0)); ttk.Label(prompt_settings,text="人格").pack(side=tk.LEFT); ttk.Entry(prompt_settings,textvariable=self.personality,width=20).pack(side=tk.LEFT,padx=5); ttk.Label(prompt_settings,text="目的").pack(side=tk.LEFT); ttk.Entry(prompt_settings,textvariable=self.purpose,width=34).pack(side=tk.LEFT,padx=5)
        ttk.Label(frame,text="画面観測JSON").pack(anchor=tk.W)
        ttk.Button(frame,text="画面取得（Windows）",command=self.capture_screen).pack(anchor=tk.W)
        self.obs=tk.Text(frame,height=10); self.obs.pack(fill=tk.BOTH,expand=True); self.obs.insert("1.0",json.dumps({"screen_id":"title","width":1280,"height":720,"ocr_text":["NEW GAME","OPTION"]},ensure_ascii=False,indent=2))
        ttk.Label(frame,text="Automation候補JSON").pack(anchor=tk.W,pady=(8,0))
        self.actions=tk.Text(frame,height=10); self.actions.pack(fill=tk.BOTH,expand=True); self.actions.insert("1.0",json.dumps([{"action_id":"new-game","kind":"click","label":"NEW GAME","x":640,"y":360,"confidence":.95},{"action_id":"option","kind":"click","label":"OPTION","x":640,"y":500,"confidence":.8}],ensure_ascii=False,indent=2))
        ttk.Button(frame,text="1ステップ判断（操作は実行しない）",command=self.run).pack(anchor=tk.W,pady=8); self.result=ttk.Label(frame,text="待機中"); self.result.pack(anchor=tk.W); self.metrics=ttk.Label(frame,text="指標: 0件"); self.metrics.pack(anchor=tk.W)
    def capture_screen(self)->None:
        try:
            from ai_game_player.frame_analyzer import FrameAnalyzer
            observation=FrameAnalyzer().analyze(WindowsScreenCapture().capture(),"live")
            self.obs.delete("1.0",tk.END); self.obs.insert("1.0",json.dumps(observation.to_dict(),ensure_ascii=False,indent=2))
        except Exception as exc: messagebox.showerror("画面取得エラー",str(exc))
    def run(self)->None:
        try:
            self.config_store.save(AppConfig(self.provider.get(),self.model.get(),self.endpoint.get(),self.personality.get(),self.purpose.get()))
            observation=ScreenObservation(**json.loads(self.obs.get("1.0",tk.END))); candidates=[ActionCandidate.from_dict(x) for x in json.loads(self.actions.get("1.0",tk.END))]
            provider=OllamaProvider(self.model.get(),self.endpoint.get()) if self.provider.get()=="Ollama" else RuleProvider()
            pipeline=DecisionPipeline(MemorySource(observation,candidates),Path("data/games/sandbox"),provider); decision=pipeline.run(purpose=self.purpose.get(),personality=self.personality.get())
            self.result.config(text=f"選択: {decision.action_id} / {decision.reason}"); m=MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load()); self.metrics.config(text=f"指標: total={m.total}, dry-run={m.dry_run}, executed={m.executed}, failed={m.failed}")
        except Exception as exc: messagebox.showerror("判断エラー",str(exc))

def main()->None:
    root=tk.Tk(); Application(root); root.mainloop()
if __name__ == "__main__": main()

