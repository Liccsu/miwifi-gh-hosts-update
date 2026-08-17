"""轻量 WebUI: 状态展示与授权码提交 (纯标准库)。

用途:
- 展示 token 状态 / 同步结果, 便于云端容器运维观察
- token 失效需要授权时, 页面显示授权链接; 用户浏览器完成登录后,
  把回跳 URL 粘贴到页面输入框提交, 程序自动换取并缓存新 token,
  无需在宿主机手动创建文件

安全:
- 默认无鉴权 (适合内网/私有云); 设置 WEBUI_TOKEN 后所有请求需携带
  token 参数 (URL ?token= 或 X-Token 请求头)
"""

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MiWiFi Hosts Sync</title>
<style>
body{font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;padding:16px;background:#f5f6f8;color:#222}
.card{background:#fff;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:20px}.kv{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #eee}
.kv:last-child{border:none}.label{color:#666}
.badge{padding:2px 10px;border-radius:10px;font-size:13px;color:#fff}
.badge.ok{background:#2e7d32}.badge.bad{background:#c62828}.badge.warn{background:#ef6c00}
textarea{width:100%;height:80px;box-sizing:border-box;font-family:monospace;font-size:13px}
button{background:#1565c0;color:#fff;border:none;border-radius:6px;padding:10px 20px;font-size:14px;cursor:pointer}
button:disabled{background:#aaa}
a{color:#1565c0;word-break:break-all}
.msg{padding:8px;border-radius:6px;margin-top:8px;font-size:13px}
.msg.err{background:#ffebee;color:#c62828}.msg.ok{background:#e8f5e9;color:#2e7d32}
</style>
</head>
<body>
<h1>MiWiFi GitHub Hosts 同步</h1>
<div class="card"><div class="kv"><span class="label">access_token</span><span id="token" class="badge warn">…</span></div>
<div class="kv"><span class="label">剩余有效期</span><span id="expires">—</span></div>
<div class="kv"><span class="label">同步间隔</span><span id="interval">—</span></div>
<div class="kv"><span class="label">上次同步</span><span id="last_sync">—</span></div>
<div class="kv"><span class="label">上次结果</span><span id="last_result">—</span></div>
<div class="kv"><span class="label">条目数(托管/手动)</span><span id="entries">—</span></div></div>
<div class="card" id="authcard" style="display:none">
<div class="kv"><span class="label">需要授权</span><span id="authmsg" class="badge bad">token 失效</span></div>
<p>请用浏览器打开以下链接, 登录小米账号完成授权 (新设备首次需短信验证码):</p>
<p><a id="authurl" target="_blank" href="#">加载中…</a></p>
<p>授权后浏览器跳转到 s.miwifi.com, <b>复制地址栏完整 URL</b> 粘贴到下面并提交:</p>
<textarea id="code"></textarea>
<button id="submit">提交授权</button>
<div id="msg"></div></div>
<div class="card" id="logincard" style="display:none">
<div class="kv"><span class="label">账号登录</span><span id="loginstate" class="badge warn">未开始</span></div>
<p>程序自动完成登录流程, 仅在需要短信验证码时要求你输入</p>
<button id="loginbtn">开始登录</button>
<div id="verifypanel" style="display:none;margin-top:10px">
<p id="verifyhint">验证码已发送, 请输入:</p>
<input type="text" id="verifycode" placeholder="6 位验证码">
<button id="verifybtn">提交验证码</button>
</div>
<div id="loginmsg"></div></div>
<div class="card"><button id="syncbtn">立即同步一次</button><div id="syncmsg"></div></div>
<script>
const params=new URLSearchParams(location.search);
const token=params.get('token')||'';
function api(path,opts){opts=opts||{};const h=opts.headers||{};if(token)h['X-Token']=token;
return fetch(path,Object.assign({},opts,{headers:h})).then(r=>r.json());}
function render(s){
const t=document.getElementById('token');
t.textContent=s.token_ok?'有效':'无效/缺失';t.className='badge '+(s.token_ok?'ok':'bad');
document.getElementById('expires').textContent=s.expires_in_days!=null?s.expires_in_days+' 天':'—';
document.getElementById('interval').textContent=s.sync_interval?s.sync_interval:'—';
document.getElementById('last_sync').textContent=s.last_sync||'—';
document.getElementById('last_result').textContent=s.last_result||'—';
document.getElementById('entries').textContent=(s.managed_entries!=null? s.managed_entries+' / '+s.manual_entries:'—');
if(s.auth_required){
document.getElementById('authcard').style.display='block';
document.getElementById('authurl').href=s.auth_url||'#';document.getElementById('authurl').textContent=s.auth_url||'';
}else{document.getElementById('authcard').style.display='none';}
const lc=document.getElementById('logincard');
if(s.account_login){lc.style.display='block';}else{lc.style.display='none';return;}
const ls=document.getElementById('loginstate');
const steps={idle:['未开始','warn'],working:['登录中…','warn'],verify_required:['等待验证码','bad'],error:['失败','bad'],done:['已完成','ok']};
const st=steps[s.login_step]||steps.idle;
ls.textContent=st[0];ls.className='badge '+st[1];
document.getElementById('verifypanel').style.display=s.login_step==='verify_required'?'block':'none';
if(s.masked_phone)document.getElementById('verifyhint').textContent='验证码已发送至 '+s.masked_phone+', 请输入:';
const lm=document.getElementById('loginmsg');
if(s.login_error){lm.textContent=s.login_error;lm.className='msg err';}else{lm.textContent='';}}
function refresh(){api('/api/status').then(render).catch(()=>{});}
document.getElementById('submit').onclick=()=>{
const c=document.getElementById('code').value.trim();
if(!c){document.getElementById('msg').textContent='请粘贴授权回跳 URL';document.getElementById('msg').className='msg err';return;}
document.getElementById('submit').disabled=true;
api('/api/authorize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c})})
.then(r=>{const m=document.getElementById('msg');
m.textContent=r.error||'已提交, 正在换取 token…';m.className='msg '+(r.error?'err':'ok');
if(!r.error){document.getElementById('code').value='';}
document.getElementById('submit').disabled=false;refresh();});};
document.getElementById('syncbtn').onclick=()=>{
api('/api/sync',{method:'POST'}).then(r=>{const m=document.getElementById('syncmsg');
m.textContent=r.error||'已触发同步';m.className='msg '+(r.error?'err':'ok');});};
document.getElementById('loginbtn').onclick=()=>{
api('/api/login',{method:'POST'}).then(r=>{const m=document.getElementById('loginmsg');
m.textContent=r.error||'登录已开始, 等待手机验证码…';m.className='msg '+(r.error?'err':'ok');});};
document.getElementById('verifybtn').onclick=()=>{
const c=document.getElementById('verifycode').value.trim();
if(!c){document.getElementById('loginmsg').textContent='请输入验证码';document.getElementById('loginmsg').className='msg err';return;}
api('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c})})
.then(r=>{const m=document.getElementById('loginmsg');
m.textContent=r.error||'验证码已提交';m.className='msg '+(r.error?'err':'ok');
if(!r.error)document.getElementById('verifycode').value='';});};
refresh();setInterval(refresh,5000);
</script></body></html>"""


class WebUI:
    """线程安全的 WebUI 服务与状态容器。"""

    def __init__(self, port=8080, token=None):
        self.port = port
        self.token = token or ""
        self.auth_codes = queue.Queue()
        self.verify_codes = queue.Queue()
        self.sync_requested = threading.Event()
        self.login_requested = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            "auth_required": False,
            "auth_url": None,
            "auth_error": None,
            "token_ok": False,
            "expires_in_days": None,
            "sync_interval": None,
            "last_sync": None,
            "last_result": None,
            "managed_entries": None,
            "manual_entries": None,
            "account_login": False,
            "login_step": "idle",  # idle | working | verify_required | error | done
            "masked_phone": None,
            "login_error": None,
        }
        self._server = None
        self._ready = threading.Event()

    def run(self):
        handler = lambda *args, **kw: _Handler(self, *args, **kw)
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._ready.set()
        self._server.serve_forever()

    def shutdown(self):
        if self._server:
            self._server.shutdown()

    def update(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def request_authorization(self, url):
        with self._lock:
            self._state["auth_required"] = True
            self._state["auth_url"] = url
            self._state["auth_error"] = None

    def authorization_done(self):
        with self._lock:
            self._state["auth_required"] = False
            self._state["auth_url"] = None

    def submit_code(self, url):
        """WebUI 提交授权回跳 URL; 返回错误信息或 None。"""
        if not (url or "").strip():
            return "内容为空"
        self.auth_codes.put(url.strip())
        return None

    def submit_verify_code(self, code):
        """WebUI 提交登录验证码; 返回错误信息或 None。"""
        if not (code or "").strip():
            return "内容为空"
        self.verify_codes.put(code.strip())
        return None

    def poll_verify_code(self):
        """非阻塞获取用户提交的验证码。"""
        try:
            return self.verify_codes.get_nowait()
        except queue.Empty:
            return None

    def poll_code(self):
        """非阻塞获取用户提交的授权内容。"""
        try:
            return self.auth_codes.get_nowait()
        except queue.Empty:
            return None

    def _authorized(self, handler):
        if not self.token:
            return True
        header = handler.headers.get("X-Token", "")
        query = _parse_query(handler.path).get("token", [""])[0]
        return header == self.token or query == self.token


def _parse_query(path):
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(path).query)


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, webui, *args, **kwargs):
        self.webui = webui
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, page):
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/":
            if not self.webui._authorized(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_html(_PAGE)
        elif route == "/api/status":
            if not self.webui._authorized(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(self.webui.snapshot())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.webui._authorized(self):
            self._send_json({"error": "unauthorized"}, 401)
            return
        route = self.path.split("?", 1)[0]
        if route == "/api/authorize":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                self._send_json({"error": "无效的 JSON"}, 400)
                return
            error = self.webui.submit_code(data.get("url", ""))
            if error:
                self._send_json({"error": error}, 400)
            else:
                self._send_json({"ok": True})
        elif route == "/api/sync":
            self.webui.sync_requested.set()
            self._send_json({"ok": True})
        elif route == "/api/login":
            self.webui.login_requested.set()
            self._send_json({"ok": True})
        elif route == "/api/verify":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                self._send_json({"error": "无效的 JSON"}, 400)
                return
            error = self.webui.submit_verify_code(data.get("code", ""))
            if error:
                self._send_json({"error": error}, 400)
            else:
                self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)
