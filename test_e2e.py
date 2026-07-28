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
            {
                "model": "fake-model-v1",
                "choices": [
                    {"message": {"role": "assistant", "content": "pong"}}
                ],
            }
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
    monkeypatch.setenv("SUMMARY_WORKER", "0")  # don't run the bg titler in tests
    monkeypatch.setattr(main, "MONGO_URI", mongo.get_connection_url())
    monkeypatch.setattr(main, "LMSTUDIO_URL", fake_lmstudio)
    monkeypatch.setattr(main, "_client", None)  # force reconnect to the container
    main.get_messages_collection().delete_many({})  # clean slate per test
    main.get_conversations_collection().delete_many({})
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

    # assistant message is tagged with the model that generated it; user isn't
    assert "model" not in docs[0]
    assert docs[1]["model"] == "fake-model-v1"

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


def test_list_conversations_aggregates_and_orders_by_recency(client):
    first = client.post("/chat", json={"message": "hello there"}).json()[
        "conversation_id"
    ]
    second = client.post("/chat", json={"message": "second chat"}).json()[
        "conversation_id"
    ]

    convos = client.get("/conversations").json()["conversations"]
    assert [c["conversation_id"] for c in convos] == [second, first]

    by_id = {c["conversation_id"]: c for c in convos}
    assert by_id[first]["message_count"] == 2  # user + assistant
    assert by_id[first]["preview"] == "hello there"  # first message as preview


def test_summarized_title_is_stored_and_listed(client):
    cid = client.post("/chat", json={"message": "plan a trip"}).json()[
        "conversation_id"
    ]

    # run the summarization job with a stub titler (min_messages=1 forces it)
    done = main.summarize_conversations(
        main.get_messages_collection(),
        main.get_conversations_collection(),
        titler=lambda history: "Trip planning",
        min_messages=1,
    )
    assert (cid, "Trip planning") in done

    convos = client.get("/conversations").json()["conversations"]
    item = next(c for c in convos if c["conversation_id"] == cid)
    assert item["title"] == "Trip planning"
