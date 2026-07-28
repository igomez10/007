import uuid
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

import main
from main import LLMReply, app, get_llm, get_messages_collection


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

    def _match(self, query):
        return [
            d for d in self.docs if all(d.get(k) == v for k, v in query.items())
        ]

    def find(self, query):
        return FakeCursor(self._match(query))

    def find_one(self, query):
        matched = self._match(query)
        return matched[0] if matched else None

    def distinct(self, field):
        seen = []
        for d in self.docs:
            if d.get(field) not in seen:
                seen.append(d.get(field))
        return seen

    def update_one(self, query, update, upsert=False):
        matched = self._match(query)
        set_fields = update.get("$set", {})
        if matched:
            matched[0].update(set_fields)
        elif upsert:
            self.docs.append({**query, **set_fields})


def test_chat_without_conversation_id_generates_uuid_inserts_and_calls_llm():
    fake = FakeCollection()
    llm_calls = []

    def fake_llm(history):
        llm_calls.append(history)
        return LLMReply(content="hi there", model="test-model")

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

        # only the assistant message records which model generated it
        assert "model" not in fake.docs[0]
        assert fake.docs[1]["model"] == "test-model"

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
    app.dependency_overrides[get_llm] = lambda: (
        lambda history: LLMReply(content="reply", model="test-model")
    )
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


def _add(messages, cid, content, when):
    messages.insert_one(
        {
            "conversation_id": cid,
            "role": "user",
            "content": content,
            "created_at": when,
        }
    )


def test_summarize_titles_by_message_count_and_idle_not_short_recent():
    messages = FakeCollection()
    conversations = FakeCollection()
    now = datetime(2026, 1, 1, 12, 3, 0)
    recent = datetime(2026, 1, 1, 12, 2, 30)  # 30s ago
    old = datetime(2026, 1, 1, 12, 0, 0)  # 3 min ago

    # A: enough messages, recent -> triggered by count
    for i in range(4):
        _add(messages, "A", f"a{i}", recent)
    # B: single message but idle -> triggered by idle
    _add(messages, "B", "hello", old)
    # C: single recent message -> not triggered
    _add(messages, "C", "hi", recent)

    done = main.summarize_conversations(
        messages,
        conversations,
        titler=lambda history: "T:" + history[0]["content"],
        now=now,
        min_messages=4,
        idle_seconds=120,
    )

    assert {cid for cid, _ in done} == {"A", "B"}
    assert conversations.find_one({"conversation_id": "A"})["title"] == "T:a0"
    assert conversations.find_one({"conversation_id": "B"})["title"] == "T:hello"
    assert conversations.find_one({"conversation_id": "C"}) is None


def test_summarize_is_idempotent_without_new_messages():
    messages = FakeCollection()
    conversations = FakeCollection()
    now = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(4):
        _add(messages, "A", f"a{i}", now)

    calls = []

    def titler(history):
        calls.append(1)
        return "Title"

    first = main.summarize_conversations(
        messages, conversations, titler=titler, now=now, min_messages=4
    )
    second = main.summarize_conversations(
        messages, conversations, titler=titler, now=now, min_messages=4
    )

    assert first == [("A", "Title")]
    assert second == []  # already summarized, no new messages
    assert len(calls) == 1
