import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

import main
from main import app, get_llm, get_messages_collection


class FakeCursor:
    def __init__(self, results):
        self._results = results

    def sort(self, key, direction=1):
        self._results = sorted(
            self._results, key=lambda d: d[key], reverse=direction == -1
        )
        return self

    def __iter__(self):
        return iter(self._results)


class FakeCollection:
    """Minimal in-memory stand-in for a pymongo collection."""

    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=len(self.docs))

    def find(self, query):
        matched = [
            d for d in self.docs if all(d.get(k) == v for k, v in query.items())
        ]
        return FakeCursor(matched)


def test_chat_without_conversation_id_generates_uuid_inserts_and_calls_llm():
    fake = FakeCollection()
    llm_calls = []

    def fake_llm(history):
        llm_calls.append(history)
        return "hi there"

    app.dependency_overrides[get_messages_collection] = lambda: fake
    app.dependency_overrides[get_llm] = lambda: fake_llm
    try:
        client = TestClient(app)
        resp = client.post("/chat", json={"message": "hello"})

        assert resp.status_code == 200
        body = resp.json()

        # a brand-new conversation id was generated (valid uuid4)
        assert uuid.UUID(body["conversation_id"]).version == 4
        assert body["reply"] == "hi there"

        # one document per message: the user message and the llm reply
        assert len(fake.docs) == 2
        assert [d["role"] for d in fake.docs] == ["user", "assistant"]
        assert fake.docs[0]["content"] == "hello"
        assert fake.docs[1]["content"] == "hi there"

        # both documents share the generated conversation id (aggregatable)
        assert all(
            d["conversation_id"] == body["conversation_id"] for d in fake.docs
        )

        # LM Studio was called once, with the user message in the history
        assert len(llm_calls) == 1
        assert llm_calls[0][-1] == {"role": "user", "content": "hello"}
    finally:
        app.dependency_overrides.clear()


def test_list_messages_returns_conversation_in_order():
    fake = FakeCollection()
    app.dependency_overrides[get_messages_collection] = lambda: fake
    app.dependency_overrides[get_llm] = lambda: (lambda history: "reply")
    try:
        client = TestClient(app)
        created = client.post("/chat", json={"message": "first"})
        cid = created.json()["conversation_id"]

        resp = client.get(f"/conversations/{cid}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "first"
    finally:
        app.dependency_overrides.clear()
