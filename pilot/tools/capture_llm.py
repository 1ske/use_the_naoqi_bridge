"""Fake LLM endpoint for free, offline configuration checks (not used in runs).

Records every request a harness sends (method, path, headers without credentials, JSON body) to
/cap/requests.jsonl and answers with an HTTP 400 error, so the harness stops without any model being
called. Used to see which tools, instructions and request settings a harness would send to the LLM.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET_HEADERS = {"authorization", "x-api-key", "x-goog-api-key", "cookie"}


class Handler(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")[:2000]
        headers = {k: ("<redacted>" if k.lower() in SECRET_HEADERS else v) for k, v in self.headers.items()}
        with open("/cap/requests.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "method": self.command, "path": self.path,
                                "headers": headers, "body": body}) + "\n")
        payload = json.dumps({"error": {"type": "invalid_request_error", "code": 400,
                                        "message": "capture endpoint: request recorded, no model behind it"}}).encode()
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _record

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
