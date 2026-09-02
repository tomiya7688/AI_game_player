import json
from pathlib import Path
from uuid import uuid4
class KnowledgeStore:
    def __init__(self,path:Path): self.path=path
    def add(self,category:str,subject:str,statement:str,confidence:float=1.0)->dict[str,object]:
        entries=self._read(); entry={"id":uuid4().hex,"category":category,"subject":subject,"statement":statement,"confidence":confidence}; entries.append(entry); self._write(entries); return entry
    def search(self,query:str,category:str|None=None)->list[dict[str,object]]:
        needle=query.casefold(); return [e for e in self._read() if (category is None or e.get("category")==category) and needle in (str(e.get("subject",""))+" "+str(e.get("statement",""))).casefold()]
    def _read(self)->list[dict[str,object]]:
        if not self.path.exists(): return []
        value=json.loads(self.path.read_text(encoding="utf-8"));
        if not isinstance(value,list): raise ValueError("knowledge.json must contain an array")
        return value
    def _write(self,entries:list[dict[str,object]])->None:
        self.path.parent.mkdir(parents=True,exist_ok=True); tmp=self.path.with_suffix(f".{uuid4().hex}.tmp")
        tmp.write_text(json.dumps(entries,ensure_ascii=False,indent=2),encoding="utf-8"); tmp.replace(self.path)