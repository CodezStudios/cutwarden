"""Smoke test: talk to the MCP server over stdio with raw JSON-RPC (no client SDK needed)."""
import json, os, subprocess, sys, tempfile

env = {**os.environ, "CUTWARDEN_WORKDIR": tempfile.mkdtemp(prefix="bc_rpc_")}
p = subprocess.Popen([sys.executable, "-m", "cutwarden.server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, env=env)
_id = 0
def rpc(method, params=None, notify=False):
    global _id
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        _id += 1; msg["id"] = _id
    p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
    if notify: return None
    while True:
        r = json.loads(p.stdout.readline())
        if r.get("id") == _id: return r
def call(tool, **args): return rpc("tools/call", {"name": tool, "arguments": args})

print(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})["result"]["serverInfo"])
rpc("notifications/initialized", notify=True)
tools = rpc("tools/list")["result"]["tools"]
print(len(tools), "tools:", ", ".join(t["name"] for t in tools))
def show(r):
    res = r.get("result", r)
    c = res.get("content", [{}])[0]
    return ("ERR " if res.get("isError") else "") + " ".join(c.get("text", f"<{c.get('type')}>").split())[:260]
print(show(call("create_project", name="smoke")))
print(show(call("add_layer", project="smoke", kind="text", preset="slam_in", start=0, duration=2, params={"text": "HELLO", "color": "#ff00ff"})))
print(show(call("add_layer", project="smoke", kind="text", preset="slam_in", start=0, duration=2, params={"text": "HELLO", "colour": "fg"})))
print(show(call("add_layer", project="smoke", kind="text", preset="slam_in", start=0, duration=2, params={"text": "HELLO"}, expected_revision=0)))
print(show(call("add_layer", project="smoke", kind="text", preset="slam_in", start=1, duration=2, params={"text": "X"}, expected_revision=0)))
print(show(call("render_frame", project="smoke", time=0.8, scale=0.3)))
print(show(call("validate_project", project="smoke")))
p.terminate()
