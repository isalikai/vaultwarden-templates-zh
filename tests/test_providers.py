"""Exercise the HTTP adapters against a local fake server (no real API is called)."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync  # noqa: E402

TOK = "⟦1⟧"


class Handler(BaseHTTPRequestHandler):
    calls: list = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def reply(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.calls.append((self.path, self.headers, payload))
        mode = Handler.mode
        if mode == "limited" or (mode == "github_limited" and "chat/completions" in self.path):
            return self.reply(429, {"error": "quota"}, {"Retry-After": "86400"})
        if "chat/completions" in self.path:
            if mode == "no_json_mode" and "response_format" in payload:
                return self.reply(400, {"error": "response_format unsupported"})
            user = payload["messages"][-1]["content"]
            batch = json.loads(user[user.index("{"):])
            text = "```json\n" + json.dumps({k: "译:" + v for k, v in batch.items()}, ensure_ascii=False) + "\n```"
            return self.reply(200, {"choices": [{"message": {"content": text}}]})
        user = payload["contents"][0]["parts"][0]["text"]
        batch = json.loads(user[user.index("{"):])
        text = json.dumps({k: "G:" + v for k, v in batch.items()}, ensure_ascii=False)
        return self.reply(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_port}"
        sync.GitHubModels.URL = base + "/inference/chat/completions"
        sync.Gemini.URL_TMPL = base + "/v1beta/models/{model}:generateContent"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Handler.calls, Handler.mode = [], "ok"
        import os
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["GEMINI_API_KEY"] = "test-key"

    def needed(self):
        core = "Your code is " + TOK
        return {core: sync.Segment(0, 0, "", "", core, ["{{c}}"], [])}

    def run_chain(self, providers):
        cache = {}
        n = sync.translate_missing(self.needed(), cache, providers, 0, lambda: None)
        return n, cache

    def test_github_models_happy_path_and_fenced_json(self):
        n, cache = self.run_chain([sync.GitHubModels()])
        self.assertEqual(n, 1)
        self.assertEqual(cache["Your code is " + TOK], "译:Your code is " + TOK)
        path, headers, payload = Handler.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(payload["model"], "openai/gpt-4.1-mini")
        self.assertIn("response_format", payload)

    def test_retries_without_json_mode_on_400(self):
        Handler.mode = "no_json_mode"
        n, _ = self.run_chain([sync.GitHubModels()])
        self.assertEqual(n, 1)
        self.assertEqual(len(Handler.calls), 2)
        self.assertNotIn("response_format", Handler.calls[1][2])

    def test_quota_exhausted_falls_back_to_gemini(self):
        Handler.mode = "github_limited"
        n, cache = self.run_chain([sync.GitHubModels(), sync.Gemini()])
        self.assertEqual(n, 1)
        self.assertTrue(cache["Your code is " + TOK].startswith("G:"))
        self.assertEqual(len(Handler.calls), 2)  # one rejected GitHub call, one Gemini call

    def test_all_providers_limited_raises(self):
        Handler.mode = "limited"
        with self.assertRaises(sync.AllProvidersFailed):
            self.run_chain([sync.GitHubModels()])


if __name__ == "__main__":
    unittest.main()
