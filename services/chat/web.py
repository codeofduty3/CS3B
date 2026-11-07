"""Part 0: the web chat (standard library only). Run from the repository root:

    PYTHONPATH=src python -m services.chat.web --port 8023

POST /login {principal, token, mandate} sets an HttpOnly cookie holding a
random session id; the token itself never goes back to the browser. Every
POST /chat {text} is answered in the mandate bound at login. Binds to
127.0.0.1 only."""
import argparse
import json
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .session import ChatSession, load_world

PAGE = """<!doctype html><meta charset="utf-8"><title>Credit Watch chat</title>
<style>body{font:15px system-ui;max-width:720px;margin:2em auto;padding:0 16px}
#log{white-space:pre-wrap;border:1px solid #ccc;padding:8px;min-height:12em}
input,button{font:inherit}</style>
<form id="login">principal <input name="principal" value="priya">
token <input name="token" type="password"> mandate <input name="mandate" value="mandate_a">
<button>Log in</button></form>
<p id="who"></p><div id="log"></div>
<form id="chat"><input name="text" size="60" placeholder="Ask about this mandate"> <button>Send</button></form>
<script>
const $=s=>document.querySelector(s), log=t=>$("#log").textContent+=t+"\\n\\n";
async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return [r.status,await r.json()]}
$("#login").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);
 const [s,j]=await post("/login",Object.fromEntries(f));$("#who").textContent=s==200?"bound to "+j.mandate:"refused: "+j.error};
$("#chat").onsubmit=async e=>{e.preventDefault();const t=e.target.text.value;e.target.text.value="";
 log("you: "+t);const [s,j]=await post("/chat",{text:t});log(s==200?j.reply:"error: "+j.error)};
</script>"""


def make_server(world, port=8023, host="127.0.0.1"):
    sessions = {}                                     # session id -> ChatSession

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):                    # no request lines (bodies hold tokens)
            pass

        def send(self, code, body, ctype="application/json", cookie=None):
            raw = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            if cookie:
                self.send_header("Set-Cookie", f"sid={cookie}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def chat(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return sessions.get(c["sid"].value) if "sid" in c else None

        def do_GET(self):
            if self.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if self.path == "/whoami":
                s = self.chat()
                return self.send(200, {"principal": s.session.principal, "mandate": s.mandate}) if s \
                    else self.send(401, {"error": "not logged in"})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            try:
                b = self.body()
            except ValueError:
                return self.send(400, {"error": "bad json"})
            if self.path == "/login":
                try:
                    s = ChatSession(b.get("principal", ""), b.get("token", ""), b.get("mandate", ""), world)
                except PermissionError as e:
                    return self.send(403, {"error": str(e)})
                sid = secrets.token_urlsafe(24)
                sessions[sid] = s
                return self.send(200, {"mandate": s.mandate}, cookie=sid)
            if self.path == "/chat":
                s = self.chat()
                if s is None:
                    return self.send(401, {"error": "not logged in"})
                if "mandate" in b and b["mandate"] != s.mandate:   # the session's mandate wins, always
                    return self.send(403, {"error": "session is bound to one mandate: log in again"})
                reply, trace = s.ask(str(b.get("text", "")))
                return self.send(200, {"mandate": s.mandate, "reply": reply, "steps": len(trace)})
            self.send(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Credit-watch web chat")
    ap.add_argument("--port", type=int, default=8023)
    a = ap.parse_args(argv)
    srv = make_server(load_world(), a.port)
    print(f"http://127.0.0.1:{a.port}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
