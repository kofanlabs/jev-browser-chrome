"""Explicit live MCP/Chrome smoke test. Uses paid Jev calls only with --live."""

import argparse
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PAGE = b"""<!doctype html><html><head><title>Jev Mac local test</title></head>
<body><h1>Jev Mac local test</h1><p id="status">Start the test.</p>
<button id="start" onclick="document.getElementById('name').disabled=false;
 document.getElementById('finish').disabled=false;
 this.disabled=true;
 document.getElementById('status').textContent='Enter the test name then finish.'">Start test</button>
<label>Test name <input id="name" disabled></label>
<button id="finish" disabled onclick="fetch('/complete',{method:'POST',body:document.getElementById('name').value})
.then(r=>r.text())
.then(t=>{document.getElementById('status').textContent=t;
 this.disabled=true})">Finish test</button>
</body></html>"""


async def run(out):
    proof = {"completed": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):
            value = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 2000)).decode()
            proof["completed"] = self.path == "/complete" and value == "Mac bridge test"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"MAC TEST PASSED" if proof["completed"] else b"Incorrect test name")

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}/"
    evidence = {"goal_verified": False, "active_tab_changed": False, "samples": 0}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "chrome_mcp.py")])
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()

                async def call(name, args=None):
                    result = await session.call_tool(name, args or {})
                    if result.is_error:
                        raise RuntimeError(str(result.content)[:600])
                    return json.loads(next(x.text for x in result.content if x.type == "text"))

                connected = await call("jev_browser_connect")
                if not connected.get("connected"):
                    raise RuntimeError("Chrome extension is not connected")
                sys.path.insert(0, str(ROOT))
                from extension_client import call as bridge

                def active_ids():
                    data = bridge("list_tabs")
                    tabs = data if isinstance(data, list) else data.get("tabs", [])
                    return sorted(str(t["tabId"]) for t in tabs if t.get("active"))

                baseline = await asyncio.to_thread(active_ids)
                opened = await call("jev_browser_open_tab", {"url": url, "active": False})
                tab_id = opened["tab"]["tabId"]
                for _ in range(40):
                    await call("jev_browser_tabs")
                    tab = bridge("get_tab", {"tabId": tab_id})
                    if tab["url"] == url:
                        break
                    await asyncio.sleep(0.1)
                await call(
                    "jev_browser_run",
                    {
                        "goal": (
                            "Start the test, enter Mac bridge test into Test name, then finish the test. "
                            "Stop when MAC TEST PASSED is visible."
                        ),
                        "tab_id": tab_id,
                        "expected_url": url,
                        "allowed_origins": [url.rstrip("/")],
                        "act": True,
                        "max_steps": 8,
                        "max_seconds": 60,
                        "capture_final": False,
                    },
                )
                deadline = time.monotonic() + 75
                while time.monotonic() < deadline:
                    evidence["samples"] += 1
                    evidence["active_tab_changed"] |= (await asyncio.to_thread(active_ids)) != baseline
                    state = await call("jev_browser_wait", {"seconds": 0.2})
                    if state["status"] == "needs_host":
                        await call(
                            "jev_browser_respond", {"request_id": state["request"]["id"], "text": "Mac bridge test"}
                        )
                    elif state["status"] != "running":
                        break
                else:
                    await call("jev_browser_stop")
                    raise RuntimeError("Test timed out")
                evidence.update(
                    status=state["status"],
                    final=state.get("final"),
                    history=state.get("history"),
                    error=state.get("error"),
                    seconds=state.get("elapsedSeconds"),
                )
                evidence["server_verified"] = proof["completed"]
                evidence["goal_verified"] = proof["completed"] and "MAC TEST PASSED" in state.get("final", {}).get(
                    "text", ""
                )
                evidence["background_verified"] = evidence["goal_verified"] and not evidence["active_tab_changed"]
    finally:
        http.shutdown()
        http.server_close()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))
    return evidence["background_verified"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("Use --live to authorize paid Jev calls on the local test page")
    raise SystemExit(0 if asyncio.run(run(args.out)) else 1)
