"""轻量 WebUI: 状态展示、账号登录与授权操作 (纯标准库)。

交互设计:
- 状态驱动: 页面根据 /api/status 的 login_step / auth_required 渲染
  按钮状态机, 避免重复操作
- 登录流程: 点"开始登录" -> 程序自动登录并发送验证码 -> 页面切换为
  验证码输入 -> 提交后自动完成剩余流程
- 授权流程: token 缺失/失效且无账号时, 页面展示授权链接, 用户授权后
  粘贴回跳 URL 提交

安全: 设置 WEBUI_TOKEN 后所有请求需携带 ?token= 或 X-Token 请求头。
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
:root{--bg:#f0f2f7;--card:#fff;--text:#1c2430;--muted:#667085;--line:#e6e9f0;
--primary:#2f6bff;--primary-d:#2456d6;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;
--chip-bg:#f2f4f8;--radius:14px;--shadow:0 1px 2px rgba(16,24,40,.05),0 4px 16px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#10141b;--card:#1a2029;--text:#e8ecf3;--muted:#8b94a3;
--line:#2a3240;--chip-bg:#232b37;--shadow:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.3)}}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);padding:20px 14px 40px}
.wrap{max-width:680px;margin:0 auto}
.hero{background:linear-gradient(135deg,#2f6bff 0%,#7b5cff 60%,#a855f7 100%);border-radius:var(--radius);
padding:22px 20px;color:#fff;margin-bottom:16px;box-shadow:var(--shadow)}
.hero h1{font-size:19px;font-weight:700;letter-spacing:.2px}
.hero p{font-size:13px;opacity:.85;margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
padding:16px;margin-bottom:14px;box-shadow:var(--shadow)}
.card h2{font-size:14px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.kv{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px dashed var(--line);font-size:13.5px}
.kv:last-of-type{border-bottom:none}
.kv .label{color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.15)}
.dot.bad{background:var(--bad);box-shadow:0 0 0 3px rgba(220,38,38,.15)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(217,119,6,.15);animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.bar{height:6px;background:var(--chip-bg);border-radius:3px;margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,#2f6bff,#7b5cff);transition:width .4s}
.bar i.warn{background:linear-gradient(90deg,#d97706,#ea580c)}
.bar i.bad{background:linear-gradient(90deg,#dc2626,#ef4444)}
button{background:var(--primary);color:#fff;border:none;border-radius:9px;padding:10px 18px;font-size:14px;
font-weight:600;cursor:pointer;transition:background .15s,transform .05s}
button:hover:not(:disabled){background:var(--primary-d)}
button:active:not(:disabled){transform:translateY(1px)}
button:disabled{opacity:.55;cursor:not-allowed}
button.ghost{background:transparent;color:var(--primary);border:1px solid var(--primary)}
button.ghost:hover:not(:disabled){background:rgba(47,107,255,.08)}
.steps{display:flex;gap:6px;margin:10px 0;font-size:12px;color:var(--muted);flex-wrap:wrap}
.steps span{background:var(--chip-bg);padding:4px 10px;border-radius:20px}
.steps b{color:var(--text)}
textarea{width:100%;min-height:72px;border:1px solid var(--line);border-radius:9px;padding:10px;
font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:var(--card);color:var(--text);resize:vertical}
input[type=text]{width:100%;max-width:220px;border:1px solid var(--line);border-radius:9px;padding:10px;
font-size:15px;letter-spacing:3px;text-align:center;background:var(--card);color:var(--text)}
a{color:var(--primary);word-break:break-all;font-size:12.5px;line-height:1.5}
.hint{font-size:12.5px;color:var(--muted);margin:8px 0;line-height:1.6}
.err{color:var(--bad);font-size:13px;margin-top:8px}
.oktxt{color:var(--ok);font-size:13px;margin-top:8px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.35);
border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
#toast{position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-80px);z-index:99;
background:#1c2430;color:#fff;padding:10px 18px;border-radius:10px;font-size:13.5px;opacity:0;
transition:transform .25s,opacity .25s;box-shadow:0 6px 20px rgba(0,0,0,.25);max-width:90vw;text-align:center}
#toast.show{transform:translateX(-50%) translateY(0);opacity:1}
#toast.ok{background:#16a34a}#toast.err{background:#dc2626}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="wrap">
<div class="hero"><h1>MiWiFi GitHub Hosts 同步</h1><p>自动同步 GitHub-IP-hosts 到路由器自定义 Hosts</p></div>

<div class="card">
<h2>同步状态</h2>
<div class="kv"><span class="label">access_token</span><span id="token"><span class="dot warn"></span>加载中…</span></div>
<div class="kv"><span class="label">剩余有效期</span><span id="expires">—</span></div>
<div id="expbarwrap" class="hidden"><div class="bar"><i id="expbar"></i></div></div>
<div class="kv"><span class="label">同步间隔</span><span id="interval">—</span></div>
<div class="kv"><span class="label">上次同步</span><span id="last_sync">—</span></div>
<div class="kv"><span class="label">上次结果</span><span id="last_result">—</span></div>
<div class="kv"><span class="label">条目数 (托管 / 手动)</span><span id="entries">—</span></div>
<div class="row"><button id="syncbtn">立即同步</button><span id="syncstate" class="hint" style="margin:0"></span></div>
</div>

<div class="card hidden" id="logincard">
<h2>账号登录</h2>
<p class="hint" id="loginhint">登录流程由程序自动完成, 仅需在收到短信验证码时输入一次</p>
<div class="row">
<button id="loginbtn">开始登录</button>
<span id="loginstate" class="hint" style="margin:0"></span>
</div>
<div id="verifypanel" class="hidden">
<p class="hint" id="verifyhint"></p>
<div class="row"><input type="text" id="verifycode" maxlength="8" inputmode="numeric" placeholder="验证码">
<button id="verifybtn">提交验证码</button></div>
</div>
<div id="loginmsg"></div>
</div>

<div class="card hidden" id="authcard">
<h2>需要授权</h2>
<p class="hint">请在浏览器打开以下链接并登录小米账号 (新设备首次需短信验证码确认)。授权后复制地址栏完整 URL 粘贴到下方提交。</p>
<div class="steps"><span><b>1</b> 打开授权链接</span><span><b>2</b> 登录并授权</span><span><b>3</b> 粘贴回跳 URL</span></div>
<a id="authurl" target="_blank" href="#">加载中…</a>
<div class="row"><textarea id="code" placeholder="粘贴 http://s.miwifi.com/... 完整 URL"></textarea></div>
<div class="row"><button id="submit">提交授权</button><span id="authstate" class="hint" style="margin:0"></span></div>
<div id="authmsg"></div>
</div>
</div>

<div id="toast"></div>
<script>
const params=new URLSearchParams(location.search);
const TOKEN=params.get('token')||'';
function api(path,opts){opts=opts||{};const h=opts.headers||{};if(TOKEN)h['X-Token']=TOKEN;
return fetch(path,Object.assign({},opts,{headers:h})).then(r=>r.json().catch(()=>({error:'响应解析失败'})));}
let toastTimer=null;
function toast(msg,type){const t=document.getElementById('toast');t.textContent=msg;
t.className='show '+(type||'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='',2600);}
function setBtn(btn,busy,label){btn.disabled=!!busy;if(label)btn.textContent=label;
return busy?'<span class="spinner"></span>':'';}

const STEPS={idle:['',''],working:['登录中…','登录进行中'],verify_required:['重新发送验证码','等待验证码'],
error:['重试登录','上次登录失败'],done:['已登录','登录完成']};
let curTokenOk=false;
function render(s){
curTokenOk=!!s.token_ok;
const t=document.getElementById('token');
if(s.token_ok){t.innerHTML='<span class="dot ok"></span>有效';}
else{t.innerHTML='<span class="dot bad"></span>无效 / 缺失';}
document.getElementById('expires').textContent=s.expires_in_days!=null?s.expires_in_days+' 天':'—';
const eb=document.getElementById('expbarwrap'),bar=document.getElementById('expbar');
if(s.expires_in_days!=null){eb.classList.remove('hidden');
const pct=Math.max(s.expires_in_days/90*100,2);
bar.style.width=pct+'%';
bar.className=pct<15?'bad':(pct<35?'warn':'');}else{eb.classList.add('hidden');}
document.getElementById('interval').textContent=s.sync_interval||'—';
document.getElementById('last_sync').textContent=s.last_sync||'—';
document.getElementById('last_result').textContent=s.last_result||'—';
document.getElementById('entries').textContent=(s.managed_entries!=null?s.managed_entries+' / '+s.manual_entries:'—');
const lc=document.getElementById('logincard');
if(s.account_login){
lc.classList.remove('hidden');
const step=STEPS[s.login_step]||STEPS.idle;
const lb=document.getElementById('loginbtn');
if(s.login_step==='idle'){
// token 有效时登录为可选操作, 弱化为"更新 token"
lb.textContent=s.token_ok?'更新 token':'开始登录';
lb.disabled=false;
lb.className=s.token_ok?'ghost':'';
}else{lb.className='';lb.textContent=step[0];}
lb.disabled=(s.login_step==='working');
document.getElementById('loginstate').textContent=s.login_step==='idle'?'':step[1];
const vp=document.getElementById('verifypanel');
vp.classList.toggle('hidden',s.login_step!=='verify_required');
if(s.masked_phone)document.getElementById('verifyhint').textContent='验证码已发送至 '+s.masked_phone+', 请输入:';
const lm=document.getElementById('loginmsg');
if(s.login_error){lm.textContent='✕ '+s.login_error;lm.className='err';}else if(s.login_step==='done'){lm.textContent='✓ 登录完成, token 已更新';lm.className='oktxt';}else{lm.textContent='';}
}else{lc.classList.add('hidden');}
const ac=document.getElementById('authcard');
if(s.auth_required){ac.classList.remove('hidden');
const au=document.getElementById('authurl');au.href=s.auth_url||'#';au.textContent=s.auth_url||'';
const am=document.getElementById('authmsg');
if(s.auth_error){am.textContent='✕ '+s.auth_error;am.className='err';}else{am.textContent='';}
}else{ac.classList.add('hidden');}
}
function refresh(){api('/api/status').then(render).catch(()=>{});}

document.getElementById('loginbtn').onclick=()=>{
if(curTokenOk&&!confirm('access_token 仍有效。确认要现在刷新吗? 将发送验证码短信到你的手机, 刷新后旧 token 立即作废。'))return;
api('/api/login',{method:'POST'}).then(r=>{
if(r.error){toast(r.error,'err');}
else{toast('已开始登录, 请留意手机验证码','ok');refresh();}});};
document.getElementById('verifybtn').onclick=()=>{
const c=document.getElementById('verifycode').value.trim();
if(!c){toast('请输入验证码','err');return;}
const btn=document.getElementById('verifybtn');
setBtn(btn,true,'提交中…');
api('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c})})
.then(r=>{setBtn(btn,false,'提交验证码');
if(r.error){toast(r.error,'err');}else{document.getElementById('verifycode').value='';toast('验证码已提交','ok');refresh();}});};
document.getElementById('submit').onclick=()=>{
const c=document.getElementById('code').value.trim();
if(!c){toast('请粘贴授权回跳 URL','err');return;}
const btn=document.getElementById('submit');
setBtn(btn,true,'提交中…');
api('/api/authorize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c})})
.then(r=>{setBtn(btn,false,'提交授权');
if(r.error){toast(r.error,'err');}else{document.getElementById('code').value='';toast('已提交, 正在换取 token…','ok');refresh();}});};
let syncing=false;
document.getElementById('syncbtn').onclick=()=>{
if(syncing)return;syncing=true;
const btn=document.getElementById('syncbtn');
setBtn(btn,true,'同步中…');
api('/api/sync',{method:'POST'}).then(r=>{
setBtn(btn,false,'立即同步');syncing=false;
toast(r.error||'已触发同步',r.error?'err':'ok');});};
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
            self._state["auth_error"] = None

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

    def poll_code(self):
        """非阻塞获取用户提交的授权内容。"""
        try:
            return self.auth_codes.get_nowait()
        except queue.Empty:
            return None

    def poll_verify_code(self):
        """非阻塞获取用户提交的验证码。"""
        try:
            return self.verify_codes.get_nowait()
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
