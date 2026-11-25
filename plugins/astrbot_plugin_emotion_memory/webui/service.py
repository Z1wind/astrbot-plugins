# -*- coding: utf-8 -*-
"""情感记忆 webUI：本地管理页面，维护 emotion_memories.json（对象 + 别名 + 档案）。

用法（Windows，隐藏窗口后台启动）：
    Start-Process python -ArgumentList '<本文件路径>' -WindowStyle Hidden
浏览器打开 http://127.0.0.1:8877
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

DATA_FILE = Path(__file__).resolve().parent.parent / "emotion_memories.json"
HOST, PORT = "127.0.0.1", 8877
_LOCK = threading.Lock()

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>乐乐 · 情感记忆管理台</title>
<style>
  :root{--bg:#0f1420;--card:#1a2233;--line:#2a3650;--txt:#e6edf7;--mut:#8ea2c0;--acc:#4f8cff;--del:#ff5c6c;}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif;padding:24px}
  h1{font-size:20px;margin:0 0 4px} .sub{color:var(--mut);margin-bottom:20px;font-size:12px}
  .toolbar{display:flex;gap:8px;margin-bottom:16px}
  input,textarea{background:#0d1420;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px 10px;font-size:13px}
  input{flex:1} textarea{width:100%;min-height:180px;resize:vertical}
  button{background:var(--acc);border:none;color:#fff;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--mut)}
  button.danger{background:var(--del)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card h3{margin:0 0 6px;font-size:15px}
  .alias span{display:inline-block;background:#24334f;border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0;font-size:12px}
  .alias .x{cursor:pointer;color:var(--del);margin-left:4px}
  .record{white-space:pre-wrap;color:var(--mut);font-size:12px;max-height:120px;overflow:auto;margin:10px 0;border-top:1px dashed var(--line);padding-top:8px}
  .card .ops{display:flex;gap:6px;margin-top:8px}
  .modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);align-items:center;justify-content:center}
  .modal.open{display:flex}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:560px;max-width:92vw;max-height:88vh;overflow:auto}
  .panel h2{margin:0 0 12px;font-size:17px}
  .row{display:flex;gap:6px;margin-bottom:8px;align-items:center}
  .muted{color:var(--mut);font-size:12px}
  .hidden{display:none}
</style>
</head>
<body>
  <h1>乐乐 · 情感记忆管理台</h1>
  <div class="sub">管理情感咨询对象档案与别名 · 数据文件 emotion_memories.json（与乐乐实时共用）</div>

  <div class="toolbar">
    <input id="q" placeholder="搜索主名 / 别名…">
    <button class="ghost" onclick="load()">刷新</button>
    <button onclick="openNew()">＋ 新增对象</button>
  </div>

  <div class="grid" id="grid"></div>

  <div class="modal" id="modal">
    <div class="panel">
      <h2 id="mTitle">编辑对象</h2>
      <div class="row"><label style="min-width:52px">主名</label><input id="fName" placeholder="例如 对象A"></div>
      <div class="row"><label style="min-width:52px">别名</label>
        <input id="fAlias" placeholder="输入外号/其他称呼，回车添加" onkeydown="if(event.key==='Enter')addAlias()">
        <button class="ghost" onclick="addAlias()">添加</button>
      </div>
      <div class="alias" id="aliasList"></div>
      <div class="muted" style="margin:8px 0 4px">人物档案</div>
      <textarea id="fRecord" placeholder="ta是谁、和咨询者关系、关键事件、给过的建议、最新进展……"></textarea>
      <div class="ops" style="margin-top:14px;justify-content:flex-end">
        <button class="ghost" onclick="closeModal()">取消</button>
        <button onclick="saveCurrent()">保存</button>
      </div>
    </div>
  </div>

<script>
const $=s=>document.querySelector(s);
let current=null, editingName=null;

async function api(path,method,body){
  const opt={method:method||'GET',headers:{'Content-Type':'application/json'}};
  if(body)opt.body=JSON.stringify(body);
  const r=await fetch(path,opt); const t=await r.text();
  if(!r.ok) throw new Error(t||r.statusText);
  try{return t?JSON.parse(t):null}catch(e){return null}
}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

async function load(){
  const data=await api('/api/objects');
  const q=($('#q').value||'').trim().toLowerCase();
  const objs=Object.keys(data).map(n=>({name:n,...data[n]}))
    .filter(o=>!q||o.name.toLowerCase().includes(q)||(o.aliases||[]).some(a=>a.toLowerCase().includes(q)));
  $('#grid').innerHTML=objs.length?objs.map(o=>`
    <div class="card">
      <h3>${esc(o.name)}</h3>
      <div class="alias">${(o.aliases||[]).map(a=>`<span>${esc(a)}</span>`).join('')||'<span class="muted">无别名</span>'}</div>
      <div class="record">${esc(o.record||'')}</div>
      <div class="ops">
        <button class="ghost" onclick="openEdit('${esc(o.name)}')">编辑</button>
        <button class="danger" onclick="delObj('${esc(o.name)}')">删除</button>
      </div>
    </div>`).join('') : '<div class="muted">暂无对象，点右上角「新增对象」。</div>';
}
async function openNew(){
  editingName=null; current={name:'',aliases:[]};
  $('#mTitle').textContent='新增对象';
  $('#fName').value=''; $('#fRecord').value='';
  renderAliases(); $('#modal').classList.add('open');
}
async function openEdit(n){
  editingName=n; current=await api('/api/objects/'+encodeURIComponent(n));
  $('#mTitle').textContent='编辑 · '+n;
  $('#fName').value=current.name; $('#fRecord').value=current.record||'';
  renderAliases(); $('#modal').classList.add('open');
}
function closeModal(){ $('#modal').classList.remove('open'); }
function renderAliases(){
  $('#aliasList').innerHTML=(current.aliases||[]).map(a=>`<span>${esc(a)}<b class="x" onclick="delAlias('${esc(a)}')">✕</b></span>`).join('');
}
async function addAlias(){
  const a=($('#fAlias').value||'').trim(); if(!a)return;
  const body=await api('/api/objects/'+encodeURIComponent(editingName)+'/aliases',{name:a});
  await api('/api/objects/'+encodeURIComponent(editingName)+'/aliases', 'POST', {alias:a});
  current=await api('/api/objects/'+encodeURIComponent(editingName));
  $('#fAlias').value=''; renderAliases(); load();
}
async function delAlias(a){
  await api('/api/objects/'+encodeURIComponent(editingName)+'/aliases/'+encodeURIComponent(a),'DELETE');
  current=await api('/api/objects/'+encodeURIComponent(editingName));
  renderAliases(); load();
}
async function saveCurrent(){
  const name=($('#fName').value||'').trim(); const record=($('#fRecord').value||'').trim();
  if(!name){alert('主名不能为空');return}
  if(editingName && name!==editingName){ // 重命名：先建新的再删旧的
    await api('/api/objects', 'POST', {name,record,aliases:(current.aliases||[])});
    await api('/api/objects/'+encodeURIComponent(editingName),'DELETE');
  }else{
    await api('/api/objects', 'POST', {name,record,aliases:(current.aliases||[])});
  }
  closeModal(); load();
}
async function delObj(n){
  if(!confirm('确定删除「'+n+'」的档案？'))return;
  await api('/api/objects/'+encodeURIComponent(n),'DELETE'); load();
}
$('#q').addEventListener('input',load);
load();
</script>
</body>
</html>
"""


def _load():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(mem):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    tmp.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}

    def do_GET(self):
        p = unquote(urlparse(self.path).path)
        if p == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if p == "/api/objects":
            with _LOCK:
                return self._send(200, _load())
        parts = p.split("/")  # ["", "api", "objects", name]
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "objects":
            with _LOCK:
                mem = _load()
                if parts[3] not in mem:
                    return self._send(404, {"error": "not found"})
                return self._send(200, {"name": parts[3], **mem[parts[3]]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = unquote(urlparse(self.path).path)
        with _LOCK:
            mem = _load()
            if p == "/api/objects":
                body = self._read_json()
                name = (body.get("name") or "").strip()
                if not name:
                    return self._send(400, {"error": "name required"})
                entry = mem.setdefault(name, {"record": "", "aliases": []})
                if "record" in body:
                    entry["record"] = (body.get("record") or "").strip()
                if "aliases" in body:
                    entry["aliases"] = [a for a in body.get("aliases", []) if str(a).strip()]
                _save(mem)
                return self._send(200, {"name": name, **entry})
            parts = p.split("/")
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "objects" and parts[4] == "aliases":
                name = parts[3]
                if name not in mem:
                    return self._send(404, {"error": "not found"})
                body = self._read_json()
                alias = (body.get("alias") or "").strip()
                if not alias:
                    return self._send(400, {"error": "alias required"})
                if alias not in mem[name]["aliases"]:
                    mem[name]["aliases"].append(alias)
                _save(mem)
                return self._send(200, {"name": name, **mem[name]})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        p = unquote(urlparse(self.path).path)
        with _LOCK:
            mem = _load()
            parts = p.split("/")  # ["","api","objects",name,"aliases",alias]
            if len(parts) == 4 and parts[1] == "api" and parts[2] == "objects":
                name = parts[3]
                if name in mem:
                    del mem[name]
                    _save(mem)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            if len(parts) == 6 and parts[2] == "objects" and parts[4] == "aliases":
                name, alias = parts[3], parts[5]
                if name in mem:
                    mem[name]["aliases"] = [a for a in mem[name].get("aliases", []) if a != alias]
                    _save(mem)
                    return self._send(200, {"name": name, **mem[name]})
                return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"[情感记忆 webUI] http://{HOST}:{PORT}  数据: {DATA_FILE}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
