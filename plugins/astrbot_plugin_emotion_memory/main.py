# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register

DATA_DIR = Path(__file__).parent
DATA_FILE = DATA_DIR / "emotion_memories.json"

# ---- 数据访问（threading.Lock 保护，供 LLM 工具与 web handler 共用） ----
_IO_LOCK = threading.Lock()


def _load_mem() -> dict:
    """加载记忆，兼容新旧格式：{"名":"档案"} 或 {"名":{"record":..,"aliases":[..]}}。"""
    with _IO_LOCK:
        try:
            if DATA_FILE.exists():
                raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                mem: dict = {}
                for k, v in raw.items():
                    if isinstance(v, dict) and "record" in v:
                        mem[k] = {
                            "record": v.get("record", ""),
                            "aliases": list(v.get("aliases", [])),
                        }
                    else:
                        mem[k] = {"record": str(v), "aliases": []}
                return mem
        except Exception as e:  # noqa: BLE001
            logger.error(f"[情感记忆] 读取档案失败: {e}")
        return {}


def _save_mem(mem: dict) -> None:
    with _IO_LOCK:
        tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps(mem, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(DATA_FILE)


def _primary_for(mem: dict, name: str) -> str | None:
    """如果 name 是某对象的别名，返回其主名；否则返回 name。"""
    if name in mem:
        return name
    for primary, entry in mem.items():
        if name in entry.get("aliases", []):
            return primary
    return None


@register(
    "astrbot_plugin_emotion_memory",
    "your_name",
    "情感咨询专用记忆：乐乐用 emotion_memory_save/query/list 记住情感咨询对象（谁是谁、关系、事件、建议），支持别名；配 AstrBot 内置 webUI 页面维护。跨对话持久。",
    "1.2.0",
)
class EmotionMemory(Star):
    def __init__(self, context) -> None:
        super().__init__(context)
        self._mem: dict = _load_mem()
        self._dirty = False
        self._lock = asyncio.Lock()
        self._register_web_apis()

    # ---------- 数据持久化 ----------
    async def _flush(self) -> None:
        if not self._dirty:
            return
        _save_mem(self._mem)
        self._dirty = False
        logger.info(f"[情感记忆] 已保存档案，共 {len(self._mem)} 个对象")

    # ---------- LLM 工具 ----------
    @filter.llm_tool(name="emotion_memory_save")
    async def emotion_memory_save(
        self,
        event: AstrMessageEvent,
        target: str = "",
        record: str = "",
    ) -> str:
        """记住/更新一个情感咨询对象的人物档案。当群聊里出现需要持续关注的对象（某人正在追/暗恋/失恋的人，或来求助的群友）时，把关键信息存档，之后任何时候都能回忆起来。同一对象再次保存会覆盖更新，用于记录最新进展。如果 target 是某对象的别名/外号，会自动归到它的主档案名下。

        Args:
            target(string): 对象的名字或称呼（可以是主名或别名/外号），例如“对象A”“小A”“对象B”。
            record(string): 关于这个人的完整档案，用简洁中文分条写：ta是谁、和咨询者的关系、关键事件经过、乐乐给过的判断和建议、目前最新进展。
        """
        target = (target or "").strip()
        if not target:
            return "error: 需要提供 target 对象名字。"
        record = (record or "").strip()
        if not record:
            return "error: record 档案内容不能为空。"
        async with self._lock:
            primary = _primary_for(self._mem, target) or target
            entry = self._mem.setdefault(primary, {"record": "", "aliases": []})
            entry["record"] = record
            self._dirty = True
            await self._flush()
        shown = f"「{primary}」" if primary != target else f"「{target}」"
        return f"已记住关于 {shown} 的档案（{len(record)}字）。之后群里再提到 ta，我会调用记忆工具想起来。"

    @filter.llm_tool(name="emotion_memory_query")
    async def emotion_memory_query(
        self,
        event: AstrMessageEvent,
        target: str = "",
    ) -> str:
        """查询某个情感咨询对象的人物档案。当群里聊到之前讨论过的人、想回忆 ta 是谁/发生过什么/我给过什么建议时使用。支持用别名/外号查询。

        Args:
            target(string): 要查询的对象名字或称呼（主名或别名均可）。
        """
        target = (target or "").strip()
        if not target:
            return "error: 需要提供 target 对象名字。"
        primary = _primary_for(self._mem, target)
        if primary is None:
            return f"没有找到关于「{target}」的记忆档案。如果之前没讨论过 ta，先用 emotion_memory_save 记住 ta。"
        entry = self._mem[primary]
        header = f"【{primary} 的记忆档案】"
        if target != primary:
            header = f"【{primary}（也叫 {target}）的记忆档案】"
        return f"{header}\n{entry.get('record', '')}"

    @filter.llm_tool(name="emotion_memory_list")
    async def emotion_memory_list(self, event: AstrMessageEvent) -> str:
        """列出乐乐目前记住了哪些情感咨询对象（含各自别名/外号）。想看看都记过谁，或不确定某个名字有没有记过时使用。"""
        if not self._mem:
            return "目前还没有记住任何情感咨询对象。"
        lines = []
        for name, entry in self._mem.items():
            alias = "、".join(entry.get("aliases", []))
            lines.append(name + (f"（也叫 {alias}）" if alias else ""))
        return "已记住的对象：\n" + "\n".join(lines)

    # ---------- AstrBot 内置 Web 页面 / API ----------
    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[情感记忆] 当前 AstrBot 不支持 register_web_api，webUI 不可用")
            return
        prefix = "/astrbot_plugin_emotion_memory/api"
        self.context.register_web_api(f"{prefix}/page", self._web_page, ["GET"], "情感记忆管理页")
        self.context.register_web_api(f"{prefix}/objects", self._web_objects_get, ["GET"], "情感记忆对象列表")
        self.context.register_web_api(f"{prefix}/objects", self._web_objects_post, ["POST"], "新增/更新对象")
        self.context.register_web_api(f"{prefix}/objects/<name>", self._web_object_get, ["GET"], "单个对象")
        self.context.register_web_api(f"{prefix}/objects/<name>", self._web_object_delete, ["DELETE"], "删除对象")
        self.context.register_web_api(f"{prefix}/objects/<name>/aliases", self._web_alias_add, ["POST"], "添加别名")
        self.context.register_web_api(f"{prefix}/objects/<name>/aliases/<alias>", self._web_alias_delete, ["DELETE"], "删除别名")
        # 插件页面 bridge 只有 GET/POST，删除需额外 POST 路由
        self.context.register_web_api(f"{prefix}/objects/<name>/delete", self._web_object_delete_post, ["POST"], "删除对象(POST)")
        self.context.register_web_api(f"{prefix}/objects/<name>/aliases/<alias>/delete", self._web_alias_delete_post, ["POST"], "删除别名(POST)")
        logger.info("[情感记忆] AstrBot 内置 webUI 页面与 API 已注册")

    async def _web_page(self):
        from starlette.responses import HTMLResponse
        return HTMLResponse(HTML_PAGE)

    async def _web_objects_get(self):
        from astrbot.api.web import json_response
        return json_response(dict(self._mem))

    async def _web_object_get(self, name: str):
        from astrbot.api.web import json_response
        entry = self._mem.get(name)
        if not entry:
            return json_response({"error": "not found"}, status_code=404)
        return json_response({"name": name, **entry})

    async def _web_objects_post(self):
        from astrbot.api.web import json_response, request
        data = await request.json(default={}) or {}
        name = str(data.get("name") or "").strip()
        if not name:
            return json_response({"error": "name required"}, status_code=400)
        async with self._lock:
            entry = self._mem.setdefault(name, {"record": "", "aliases": []})
            if "record" in data:
                entry["record"] = str(data.get("record") or "").strip()
            if "aliases" in data:
                entry["aliases"] = [a for a in data.get("aliases", []) if str(a).strip()]
            self._dirty = True
            await self._flush()
        return json_response({"name": name, **entry})

    async def _web_object_delete(self, name: str):
        from astrbot.api.web import json_response
        async with self._lock:
            if name in self._mem:
                del self._mem[name]
                self._dirty = True
                await self._flush()
                return json_response({"ok": True})
        return json_response({"error": "not found"}, status_code=404)

    async def _web_alias_add(self, name: str):
        from astrbot.api.web import json_response, request
        data = await request.json(default={}) or {}
        alias = str(data.get("alias") or "").strip()
        if not alias:
            return json_response({"error": "alias required"}, status_code=400)
        async with self._lock:
            if name not in self._mem:
                return json_response({"error": "not found"}, status_code=404)
            if alias not in self._mem[name]["aliases"]:
                self._mem[name]["aliases"].append(alias)
                self._dirty = True
                await self._flush()
        return json_response({"name": name, **self._mem[name]})

    async def _web_alias_delete(self, name: str, alias: str):
        from astrbot.api.web import json_response
        async with self._lock:
            if name in self._mem:
                self._mem[name]["aliases"] = [a for a in self._mem[name].get("aliases", []) if a != alias]
                self._dirty = True
                await self._flush()
                return json_response({"name": name, **self._mem[name]})
        return json_response({"error": "not found"}, status_code=404)

    async def _web_object_delete_post(self, name: str):
        return await self._web_object_delete(name)

    async def _web_alias_delete_post(self, name: str, alias: str):
        return await self._web_alias_delete(name, alias)


HTML_PAGE = """<!DOCTYPE html>
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
</style>
</head>
<body>
  <h1>乐乐 · 情感记忆管理台</h1>
  <div class="sub">AstrBot 内置插件页面 · 数据文件 emotion_memories.json（与乐乐实时共用）</div>
  <div class="toolbar">
    <input id="q" placeholder="搜索主名 / 别名…">
    <button class="ghost" onclick="load()">刷新</button>
    <button onclick="openNew()">＋ 新增对象</button>
  </div>
  <div class="grid" id="grid"></div>
  <div class="modal" id="modal"><div class="panel">
    <h2 id="mTitle">编辑对象</h2>
    <div class="row"><label style="min-width:52px">主名</label><input id="fName" placeholder="例如 对象A"></div>
    <div class="row"><label style="min-width:52px">别名</label>
      <input id="fAlias" placeholder="输入外号/其他称呼，回车添加" onkeydown="if(event.key==='Enter')addAlias()">
      <button class="ghost" onclick="addAlias()">添加</button></div>
    <div class="alias" id="aliasList"></div>
    <div class="muted" style="margin:8px 0 4px">人物档案</div>
    <textarea id="fRecord" placeholder="ta是谁、和咨询者关系、关键事件、给过的建议、最新进展……"></textarea>
    <div class="ops" style="margin-top:14px;justify-content:flex-end">
      <button class="ghost" onclick="closeModal()">取消</button>
      <button onclick="saveCurrent()">保存</button>
    </div>
  </div></div>
<script>
const API='/api/v1/plugins/extensions/astrbot_plugin_emotion_memory/api';
const $=s=>document.querySelector(s);
let current=null, editingName=null;
async function api(path,method,body){
  const opt={method:method||'GET',headers:{'Content-Type':'application/json'}};
  if(body)opt.body=JSON.stringify(body);
  const r=await fetch(API+path,opt); const t=await r.text();
  if(!r.ok) throw new Error(t||r.statusText);
  try{return t?JSON.parse(t):null}catch(e){return null}
}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function load(){
  const data=await api('/objects'); const q=($('#q').value||'').trim().toLowerCase();
  const objs=Object.keys(data).map(n=>({name:n,...data[n]}))
    .filter(o=>!q||o.name.toLowerCase().includes(q)||(o.aliases||[]).some(a=>a.toLowerCase().includes(q)));
  $('#grid').innerHTML=objs.length?objs.map(o=>`
    <div class="card"><h3>${esc(o.name)}</h3>
      <div class="alias">${(o.aliases||[]).map(a=>`<span>${esc(a)}</span>`).join('')||'<span class="muted">无别名</span>'}</div>
      <div class="record">${esc(o.record||'')}</div>
      <div class="ops"><button class="ghost" onclick="openEdit('${esc(o.name)}')">编辑</button>
      <button class="danger" onclick="delObj('${esc(o.name)}')">删除</button></div></div>`).join('')
    :'<div class="muted">暂无对象，点右上角「新增对象」。</div>';
}
async function openNew(){editingName=null;current={name:'',aliases:[]};$('#mTitle').textContent='新增对象';$('#fName').value='';$('#fRecord').value='';renderAliases();$('#modal').classList.add('open');}
async function openEdit(n){editingName=n;current=await api('/objects/'+encodeURIComponent(n));$('#mTitle').textContent='编辑 · '+n;$('#fName').value=current.name;$('#fRecord').value=current.record||'';renderAliases();$('#modal').classList.add('open');}
function closeModal(){$('#modal').classList.remove('open');}
function renderAliases(){$('#aliasList').innerHTML=(current.aliases||[]).map(a=>`<span>${esc(a)}<b class="x" onclick="delAlias('${esc(a)}')">✕</b></span>`).join('');}
async function addAlias(){const a=($('#fAlias').value||'').trim();if(!a)return;await api('/objects/'+encodeURIComponent(editingName)+'/aliases','POST',{alias:a});current=await api('/objects/'+encodeURIComponent(editingName));$('#fAlias').value='';renderAliases();load();}
async function delAlias(a){await api('/objects/'+encodeURIComponent(editingName)+'/aliases/'+encodeURIComponent(a),'DELETE');current=await api('/objects/'+encodeURIComponent(editingName));renderAliases();load();}
async function saveCurrent(){
  const name=($('#fName').value||'').trim(),record=($('#fRecord').value||'').trim();
  if(!name){alert('主名不能为空');return}
  if(editingName&&name!==editingName){await api('/objects','POST',{name,record,aliases:(current.aliases||[])});await api('/objects/'+encodeURIComponent(editingName),'DELETE');}
  else{await api('/objects','POST',{name,record,aliases:(current.aliases||[])});}
  closeModal();load();
}
async function delObj(n){if(!confirm('确定删除「'+n+'」的档案？'))return;await api('/objects/'+encodeURIComponent(n),'DELETE');load();}
$('#q').addEventListener('input',load);
load();
</script>
</body>
</html>
"""
