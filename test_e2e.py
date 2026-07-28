"""End-to-end tests: real MongoDB (testcontainers) + real HTTP to a fake
LM Studio server, exercising the full request path.

Requires a running Docker daemon. Run with: make test-e2e
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from testcontainers.community.mongodb import MongoDbContainer

import main
from main import app

pytestmark = pytest.mark.e2e


class _FakeLMStudioHandler(BaseHTTPRequestHandler):
    """Responds like LM Studio's OpenAI-compatible /v1/chat/completions."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


@pytest.fixture(scope="module")
def fake_lmstudio():
    server = HTTPServer(("127.0.0.1", 0), _FakeLMStudioHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def mongo():
    with MongoDbContainer("mongo:7") as container:
        yield container


@pytest.fixture
def client(mongo, fake_lmstudio, monkeypatch):
    monkeypatch.setattr(main, "MONGO_URI", mongo.get_connection_url())
    monkeypatch.setattr(main, "LMSTUDIO_URL", fake_lmstudio)
    monkeypatch.setattr(main, "_client", None)  # force reconnect to the container
    main.get_messages_collection().delete_many({})  # clean slate per test
    with TestClient(app) as c:
        yield c


def test_chat_persists_two_documents_and_lists_them(client):
    resp = client.post("/chat", json={"message": "ping"})
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]
    assert resp.json()["reply"] == "pong"

    # data really landed in Mongo — one document per message
    docs = list(
        main.get_messages_collection()
        .find({"conversation_id": cid})
        .sort("created_at", 1)
    )
    assert [d["role"] for d in docs] == ["user", "assistant"]
    assert [d["content"] for d in docs] == ["ping", "pong"]

    # and the list endpoint returns them in order
    msgs = client.get(f"/conversations/{cid}/messages").json()["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "ping"),
        ("assistant", "pong"),
    ]


def test_reusing_conversation_id_appends_messages(client):
    cid = client.post("/chat", json={"message": "one"}).json()["conversation_id"]
    client.post("/chat", json={"message": "two", "conversation_id": cid})

    msgs = client.get(f"/conversations/{cid}/messages").json()["messages"]
    assert [m["content"] for m in msgs] == ["one", "pong", "two", "pong"]
