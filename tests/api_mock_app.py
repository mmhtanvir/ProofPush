"""Tiny JSON API used by the API-testing tests. VARIANT = "good" (baseline) or "bad" (regressions).

Run as an app: PORT=8000 python api_mock_app.py   (VARIANT can be edited in place by a test's git commit)
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VARIANT = "good"
TOKEN = "Bearer good-token"
STARTED = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())) + ".%06dZ" % (time.time_ns() // 1000 % 1_000_000)

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Mock", "version": "1"},
    "components": {
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "Item": {"type": "object", "required": ["id", "name", "price"],
                     "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "price": {"type": "number"},
                                    "created": {"type": "string", "format": "date-time"}}},
            "NewItem": {"type": "object", "required": ["name", "price"],
                        "properties": {"name": {"type": "string", "minLength": 1}, "price": {"type": "number", "minimum": 0}}},
        },
    },
    "paths": {
        "/api/health": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/api/items": {
            "get": {"responses": {"200": {"description": "list", "content": {"application/json": {"schema": {
                "type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}}}}}}}},
            "post": {"security": [{"bearer": []}],
                     "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewItem"}}}},
                     "responses": {"201": {"description": "created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}},
                                   "401": {"description": "no auth"}, "422": {"description": "invalid"}}},
        },
        "/api/items/{item_id}": {"get": {"parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}, "example": 1}],
                                         "responses": {"200": {"description": "one"}, "404": {"description": "missing"}}}},
        "/api/legacy": {"get": {"responses": {"200": {"description": "old feature"}}}},
        "/api/notes/{note_id}": {"get": {"security": [{"bearer": []}],
                                         "parameters": [{"name": "note_id", "in": "path", "required": True, "schema": {"type": "integer"}, "example": 1}],
                                         "responses": {"200": {"description": "the owner's private note"}, "401": {"description": "no auth"},
                                                       "404": {"description": "not yours or missing"}}}},
        "/api/total": {"get": {"parameters": [{"name": "qty", "in": "query", "required": True, "schema": {"type": "integer"}, "example": 2}],
                               "responses": {"200": {"description": "price x qty"}}}},
    },
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        bad = VARIANT == "bad"
        item = {"id": 1, "name": "Pen", "price": "2.50" if bad else 2.5, "created": STARTED}
        if url.path == "/openapi.json":
            return self.send(200, SPEC)
        if url.path == "/api/health":
            return self.send(200, {"ok": True})
        if url.path == "/api/items":
            return self.send(200, {"items": [item]})
        if url.path.startswith("/api/items/"):
            return self.send(200, item) if url.path.endswith("/1") else self.send(404, {"detail": "not found"})
        if url.path == "/api/notes/1":
            auth = self.headers.get("Authorization")
            if auth not in (TOKEN, "Bearer other-token"):
                return self.send(401, {"detail": "auth required"})
            if auth != TOKEN and not bad:  # bad: any logged-in user can read someone else's note
                return self.send(404, {"detail": "not found"})
            return self.send(200, {"id": 1, "owner": "a", "text": "private"})
        if url.path == "/api/legacy" and not bad:
            return self.send(200, {"ok": True})
        if url.path == "/api/total":
            try:
                qty = int(parse_qs(url.query).get("qty", ["1"])[0])
            except ValueError:
                return self.send(422, {"detail": "qty must be an integer"})
            return self.send(200, {"total": 2.5 if bad else 2.5 * qty})  # bad: the old "ignores quantity" bug is back
        if url.path == "/slow":
            time.sleep(3)
            return self.send(200, {})
        return self.send(404, {"detail": "not found"})

    def do_POST(self):
        bad = VARIANT == "bad"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if urlparse(self.path).path != "/api/items":
            return self.send(404, {})
        if self.headers.get("Authorization") != TOKEN and not bad:
            return self.send(401, {"detail": "auth required"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self.send(400, {"detail": "malformed"})
        ok = (isinstance(body, dict) and isinstance(body.get("name"), str) and body["name"]
              and isinstance(body.get("price"), (int, float)) and not isinstance(body.get("price"), bool) and body["price"] >= 0)
        if not ok and not bad:
            return self.send(422, {"detail": "invalid"})
        return self.send(201, {"id": 2, "name": body.get("name") if isinstance(body, dict) else None, "price": 1.0, "created": STARTED})


VARIANT_BY_SERVER = {}


class VariantHandler(Handler):
    """Picks the variant by the port it was reached on, so one process can serve both versions."""

    def handle_one_request(self):
        global VARIANT
        VARIANT = VARIANT_BY_SERVER.get(self.server.server_port, VARIANT)
        super().handle_one_request()


def serve_variant(variant: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), VariantHandler)
    VARIANT_BY_SERVER[server.server_port] = variant
    return server


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
