import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "app")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")

app = FastAPI(title="Chat Server")

_client: Optional[MongoClient] = None


def get_messages_collection() -> Collection:
    """FastAPI dependency: the `messages` collection. Overridable in tests."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client[MONGO_DB]["messages"]


def call_lmstudio(history: list[dict]) -> str:
    """Send the conversation history to LM Studio's OpenAI-compatible API."""
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{LMSTUDIO_URL}/v1/chat/completions",
            json={"model": LMSTUDIO_MODEL, "messages": history},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def get_llm():
    """FastAPI dependency wrapping the LLM caller. Overridable in tests."""
    return call_lmstudio


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


@app.get("/hello")
def hello(name: str = "world"):
    return {"message": f"Hello, {name}!"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def chat(
    req: ChatRequest,
    messages: Collection = Depends(get_messages_collection),
    llm=Depends(get_llm),
):
    conversation_id = req.conversation_id or str(uuid.uuid4())

    # one document per message — store the incoming user message first
    messages.insert_one(
        {
            "conversation_id": conversation_id,
            "role": "user",
            "content": req.message,
            "created_at": datetime.now(timezone.utc),
        }
    )

    # rebuild the ordered history for this conversation and ask LM Studio
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in messages.find({"conversation_id": conversation_id}).sort(
            "created_at", ASCENDING
        )
    ]
    answer = llm(history)

    # store the assistant reply as its own document
    messages.insert_one(
        {
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": answer,
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"conversation_id": conversation_id, "reply": answer}


@app.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    messages: Collection = Depends(get_messages_collection),
):
    docs = messages.find({"conversation_id": conversation_id}).sort(
        "created_at", ASCENDING
    )
    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "role": d["role"],
                "content": d["content"],
                "created_at": d["created_at"],
            }
            for d in docs
        ],
    }
