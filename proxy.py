#!/usr/bin/env python3
"""
Thin middleware between Claude Code and LM Studio.
Handles known issues that cause hangs:
1. Haiku background requests → fake instant responses
2. Token counting endpoint → fake responses
3. Unknown/beta endpoints → graceful handling
4. Image tool_results → relocated to user message (LM Studio rejects images inside tool_result arrays)

LM Studio's native /v1/messages endpoint handles the actual API translation.
LM Studio handles its own request queuing — no serialization lock needed.
"""

import json
import http.server
import urllib.request
import sys
import uuid
import threading
from datetime import datetime

LM_STUDIO = "http://localhost:1234"
PORT = 4000


def ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    sys.stderr.write(f"[{ts()}] {msg}\n")
    sys.stderr.flush()


def rewrite_image_tool_results(body):
    """Move images from inside tool_result.content arrays to the surrounding user message.
    LM Studio rejects image blocks inside tool_result arrays but accepts them
    as standalone content blocks in the same user message."""
    count = 0
    for msg in body.get("messages", []):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        new_content = []
        for block in msg["content"]:
            if block.get("type") != "tool_result":
                new_content.append(block)
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                new_content.append(block)
                continue
            extracted = []
            kept = []
            for item in inner:
                if item.get("type") == "image":
                    extracted.append(item)
                    count += 1
                else:
                    kept.append(item)
            if extracted:
                kept.append({"type": "text", "text": "See the image(s) below."})
                block = {**block, "content": kept}
            new_content.append(block)
            for img in extracted:
                new_content.append({"type": "text", "text": "Here is the image you wanted to look at:"})
                new_content.append(img)
        msg["content"] = new_content
    return count


def is_housekeeping_model(model):
    if not model:
        return False
    m = model.lower()
    return "haiku" in m


def fake_haiku_response(body):
    is_stream = body.get("stream", False)
    model = body.get("model", "claude-haiku-4-5-20251001")
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    if is_stream:
        events = [
            {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5},
            }},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "OK"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta",
             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        return "stream", events

    return "json", {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default logging, we use our own

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
            return
        log(f"GET {self.path} → LM Studio")
        try:
            url = f"{LM_STUDIO}{self.path}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            log(f"GET {self.path} error: {e}")
            self._json_response(404, {"error": str(e)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        path = self.path

        # Token counting → fake
        if "count_tokens" in path:
            log("Token count → fake")
            self._json_response(200, {"input_tokens": 100})
            return

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "Invalid JSON"})
            return

        model = body.get("model", "")
        is_stream = body.get("stream", False)

        # Haiku → always fake as non-streaming JSON (streaming fake hangs Claude Code)
        if is_housekeeping_model(model):
            log(f"Haiku ({model}) → fake JSON")
            _, data = fake_haiku_response({"stream": False, "model": model})
            self._json_response(200, data)
            self.close_connection = True
            return

        # Relocate images out of tool_results (LM Studio compat)
        img_count = rewrite_image_tool_results(body)
        if img_count:
            log(f"Relocated {img_count} image(s) from tool_result → user message")

        # Boost max_tokens for thinking models
        orig_max = body.get("max_tokens", 4096)
        body["max_tokens"] = max(orig_max * 3, 16384)
        raw = json.dumps(body).encode()

        log(f"→ LM Studio | model={model} stream={is_stream} "
            f"msgs={len(body.get('messages', []))} "
            f"max_tokens={orig_max}→{body['max_tokens']}")

        # Forward to LM Studio
        url = f"{LM_STUDIO}{path}"
        req = urllib.request.Request(url, data=raw, method="POST")
        req.add_header("Content-Type", "application/json")
        for k in self.headers:
            if k.lower() not in ("host", "content-length", "transfer-encoding"):
                req.add_header(k, self.headers[k])

        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except Exception as e:
            log(f"LM Studio error: {e}")
            self._json_response(502, {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            })
            return

        if is_stream:
            self.send_response(200)
            ct = resp.headers.get("Content-Type", "text/event-stream")
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log("Client disconnected during stream")
            except Exception as e:
                log(f"Stream error: {e}")
            finally:
                resp.close()
                log("Stream complete")
        else:
            data = resp.read()
            resp.close()
            self.send_response(200)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            log("Response complete")

    def _json_response(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_events(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for evt in events:
            self.wfile.write(f"event: {evt['type']}\ndata: {json.dumps(evt)}\n\n".encode())
        self.wfile.flush()
        self.close_connection = True


class ThreadedHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    print(f"Claude Code ↔ LM Studio middleware | :{PORT} → {LM_STUDIO}")
    print(f"  Haiku interception: ON")
    print(f"  Token counting: ON")
    print(f"  No serialization lock (LM Studio handles queuing)")
    print(f"\n  export ANTHROPIC_BASE_URL=http://localhost:{PORT}")
    print("  export ANTHROPIC_AUTH_TOKEN=lmstudio")
    print("  claude --model qwen3.5-35b-a3b-mlx\n")

    server = ThreadedHTTPServer(("0.0.0.0", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
