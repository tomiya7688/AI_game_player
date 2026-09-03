import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider

class MemorySource:
    def __init__(self,observation:ScreenObservation,candidates:list[ActionCandidate]): self.observation=observation; self.candidates=candidates
    def read(self): return self.observation,self.candidates

class Application:
    def __init__(self,root:tk.Tk)->None:
        self.root=root; root.title("AI Game Player - Decision Sandbox"); root.geometry("900x650")
        frame=ttk.Frame(root,padding=10); frame.pack(fill=tk.BOTH,expand=True)
        self.provider= tk.StringVar(value="ローカル規則"); self.model=tk.StringVar(value="gemma3:4b"); self.endpoint=tk.StringVar(value="http://127.0.0.1:11434")
        settings=ttk.Frame(frame); settings.pack(fill=tk.X); ttk.Label(settings,text="Provider").pack(side=tk.LEFT); ttk.Combobox(settings,textvariable=self.provider,values=("ローカル規則","Ollama"),state="readonly",width=12).pack(side=tk.LEFT,padx=5); ttk.Label(settings,text="モデル").pack(side=tk.LEFT); ttk.Entry(settings,textvariable=self.model,width=16).pack(side=tk.LEFT,padx=5); ttk.Label(settings,text="Endpoint").pack(side=tk.LEFT); ttk.Entry(settings,textvariable=self.endpoint,width=28).pack(side=tk.LEFT,padx=5)
        ttk.Label(frame,text="画面観測JSON（現段階では手入力）").pack(anchor=tk.W)
        self.obs=tk.Text(frame,height=10); self.obs.pack(fill=tk.BOTH,expand=True); self.obs.insert("1.0",json.dumps({"screen_id":"title","width":1280,"height":720,"ocr_text":["NEW GAME","OPTION"]},ensure_ascii=False,indent=2))
        ttk.Label(frame,text="Automation候補JSON").pack(anchor=tk.W,pady=(8,0))
        self.actions=tk.Text(frame,height=10); self.actions.pack(fill=tk.BOTH,expand=True); self.actions.insert("1.0",json.dumps([{"action_id":"new-game","kind":"click","label":"NEW GAME","x":640,"y":360,"confidence":.95},{"action_id":"option","kind":"click","label":"OPTION","x":640,"y":500,"confidence":.8}],ensure_ascii=False,indent=2))
        ttk.Button(frame,text="1ステップ判断（操作は実行しない）",command=self.run).pack(anchor=tk.W,pady=8); self.result=ttk.Label(frame,text="待機中"); self.result.pack(anchor=tk.W)
    def run(self)->None:
        try:
            observation=ScreenObservation(**json.loads(self.obs.get("1.0",tk.END))); candidates=[ActionCandidate.from_dict(x) for x in json.loads(self.actions.get("1.0",tk.END))]
            provider=OllamaProvider(self.model.get(),self.endpoint.get()) if self.provider.get()=="Ollama" else RuleProvider()
            pipeline=DecisionPipeline(MemorySource(observation,candidates),Path("data/games/sandbox"),provider); decision=pipeline.run(purpose="画面の役割を理解する",personality="好奇心旺盛")
            self.result.config(text=f"選択: {decision.action_id} / {decision.reason}")
        except Exception as exc: messagebox.showerror("判断エラー",str(exc))

def main()->None:
    root=tk.Tk(); Application(root); root.mainloop()
if __name__ == "__main__": main()