from __future__ import annotations
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from database import CollationDB, DomainError

BASE=Path(__file__).resolve().parent; DB_PATH=os.environ.get("COLLATION_DB",str(BASE/"collation.db"))
class Handler(BaseHTTPRequestHandler):
    db=CollationDB(DB_PATH)
    def log_message(self,fmt,*args): return
    def _json(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        n=int(self.headers.get("Content-Length",0))
        try: b=json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as exc: raise DomainError("请求体必须是 JSON") from exc
        if not isinstance(b,dict): raise DomainError("请求体必须是对象")
        return b
    def do_GET(self):
        p=urlparse(self.path); parts=[x for x in p.path.split("/") if x]
        try:
            if p.path in ("/","/index.html"):
                data=(BASE/"static"/"index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
            if p.path=="/api/state": return self._json(200,self.db.snapshot())
            if len(parts)==5 and parts[:2]==["api","passages"] and parts[3]=="snapshots":
                uid=int(parse_qs(p.query).get("user_id",[0])[0]); return self._json(200,self.db.get_snapshot(int(parts[2]),int(parts[4]),uid))
            if len(parts)==4 and parts[:2]==["api","works"] and parts[3]=="collation":
                uid=int(parse_qs(p.query).get("user_id",[0])[0]); return self._json(200,self.db.export_collation(int(parts[2]),uid))
            self._json(404,{"ok":False,"error":"接口不存在"})
        except (DomainError,ValueError) as exc: self._json(400,{"ok":False,"error":str(exc)})
    def do_POST(self):
        parts=[x for x in urlparse(self.path).path.split("/") if x]; path="/"+"/".join(parts)
        try:
            b=self._body()
            if path=="/api/users": return self._json(201,{"ok":True,"id":self.db.add_user(str(b.get("name","")),str(b.get("role","editor")))})
            if path=="/api/works": return self._json(201,{"ok":True,"id":self.db.create_work(str(b.get("title","")),str(b.get("description","")),int(b.get("owner_id",0)))})
            if len(parts)==4 and parts[:2]==["api","works"] and parts[3]=="witnesses": return self._json(201,{"ok":True,"id":self.db.add_witness(int(parts[2]),str(b.get("siglum","")),str(b.get("kind","version")),str(b.get("source_note","")),str(b.get("missing_sections","")))})
            if len(parts)==4 and parts[:2]==["api","works"] and parts[3]=="passages": return self._json(201,{"ok":True,"id":self.db.add_passage(int(parts[2]),str(b.get("label","")),str(b.get("base_text","")),int(b.get("user_id",0)))})
            if len(parts)==4 and parts[:2]==["api","works"] and parts[3]=="access": self.db.grant_work_access(int(parts[2]),int(b.get("user_id",0)),str(b.get("permission","view")),int(b.get("granted_by",0))); return self._json(201,{"ok":True})
            if len(parts)==4 and parts[:2]==["api","witnesses"] and parts[3]=="editors": self.db.grant_witness_editor(int(parts[2]),int(b.get("user_id",0)),int(b.get("granted_by",0))); return self._json(201,{"ok":True})
            if path=="/api/alignments": return self._json(201,{"ok":True,"id":self.db.align_passage(int(b.get("passage_id",0)),int(b.get("witness_id",0)),str(b.get("aligned_text","")),int(b.get("sort_order",0)),int(b.get("user_id",0)))})
            if path=="/api/variants": return self._json(201,{"ok":True,"id":self.db.create_variant(int(b.get("passage_id",0)),int(b.get("witness_id",0)),str(b.get("proposed_text","")),str(b.get("reason","")),int(b.get("user_id",0)),int(b.get("expected_revision",0)))})
            if len(parts)==4 and parts[:2]==["api","variants"] and parts[3]=="revisions": return self._json(200,{"ok":True,"revision":self.db.update_variant(int(parts[2]),str(b.get("proposed_text","")),str(b.get("reason","")),int(b.get("user_id",0)),int(b.get("expected_revision",0)))})
            if path=="/api/notes": return self._json(201,{"ok":True,"id":self.db.add_note(int(b.get("variant_id",0)),str(b.get("body","")),int(b.get("user_id",0)))})
            if len(parts)==4 and parts[:2]==["api","passages"] and parts[3]=="lock": self.db.lock_passage(int(parts[2]),int(b.get("user_id",0)),str(b.get("reason",""))); return self._json(200,{"ok":True})
            self._json(404,{"ok":False,"error":"接口不存在"})
        except (DomainError,ValueError) as exc: self._json(400,{"ok":False,"error":str(exc)})
def main():
    CollationDB(DB_PATH).seed_demo(); port=int(os.environ.get("PORT","8114")); print(f"Textual collation service: http://127.0.0.1:{port}"); ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
if __name__=="__main__": main()
