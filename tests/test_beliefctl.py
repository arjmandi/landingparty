import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from worldd.beliefctl import run


def test_shell_client_sends_real_scoped_http_request_without_printing_token(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            content = json.dumps(
                {"kind": "result", "data": [{"id": "b_one", "claim": "Harbour open"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        environment = {
            **os.environ,
            "WORLD_RUN_ID": "r_shell",
            "WORLD_RUN_TOKEN": "fixture-scoped-token",
            "WORLD_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        }
        result = subprocess.run(
            [sys.executable, "-m", "worldd.beliefctl", "about", "harbour"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"][0]["id"] == "b_one"
        assert "fixture-scoped-token" not in result.stdout + result.stderr
        assert seen[0]["authorization"] == "Bearer fixture-scoped-token"
        assert seen[0]["body"] == {
            "run_id": "r_shell",
            "tool": "beliefs",
            "params": {"query": "about", "entity": "harbour"},
        }
        assert seen[0]["path"] == "/v1/act"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_missing_agent_token_fails_before_request(monkeypatch, capsys):
    monkeypatch.delenv("WORLD_RUN_TOKEN", raising=False)
    assert run(["--run-id", "r_test", "all"]) == 2
    assert "WORLD_RUN_TOKEN is required" in capsys.readouterr().err


def test_writes_use_same_action_endpoint_and_preserve_block_exit_status(monkeypatch, capsys):
    monkeypatch.setenv("WORLD_RUN_TOKEN", "fixture-token")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"kind": "blocked", "type": "policy_deny", "rule_id": "fixture"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        code = run(["--run-id", "r_test", "revise", "b_one", '{"rank": 2}'], client=client)
    assert code == 2 and seen[0]["params"] == {"id": "b_one", "delta": {"rank": 2}}
    assert json.loads(capsys.readouterr().out)["kind"] == "blocked"
