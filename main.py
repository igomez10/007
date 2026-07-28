import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection

logger = logging.getLogger("chat")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "app")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "google/gemma-4-26b-a4b")

app = FastAPI(title="Chat Server")

_client: MongoClient | None = None


def get_messages_collection() -> Collection:
    """FastAPI dependency: the `messages` collection. Overridable in tests."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client[MONGO_DB]["messages"]


class LLMError(Exception):
    """Raised when LM Studio cannot produce a reply, with a readable reason."""


def call_lmstudio(history: list[dict]) -> str:
    """Send the conversation history to LM Studio's OpenAI-compatible API.

    Raises LLMError with a specific, logged reason on any failure so the cause
    is visible in the logs and to the caller (not an opaque 500).
    """
    url = f"{LMSTUDIO_URL}/v1/chat/completions"
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                url, json={"model": LMSTUDIO_MODEL, "messages": history}
            )
    except httpx.RequestError as exc:
        # connection refused, DNS, timeout — LM Studio not reachable
        raise LLMError(
            f"could not reach LM Studio at {LMSTUDIO_URL}: {exc!r}. "
            "Is LM Studio running with the server enabled?"
        ) from exc

    if resp.is_error:
        # surface LM Studio's own error message (e.g. wrong/unloaded model)
        detail = _extract_error(resp)
        raise LLMError(
            f"LM Studio returned {resp.status_code} for model "
            f"'{LMSTUDIO_MODEL}': {detail}"
        )

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(
            f"unexpected LM Studio response shape: {resp.text[:500]!r}"
        ) from exc


def _extract_error(resp: httpx.Response) -> str:
    """Pull a human-readable message out of an LM Studio error response."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message", str(err))
        if err is not None:
            return str(err)
    return str(body)[:500]


def get_llm():
    """FastAPI dependency wrapping the LLM caller. Overridable in tests."""
    return call_lmstudio


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


@app.get("/")
def index():
    return RedirectResponse(url="/ui/")


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
    try:
        answer = llm(history)
    except LLMError as exc:
        # the user message is already persisted; log the real cause and return
        # a clear 502 so the failure is diagnosable next time it happens
        logger.error(
            "LLM call failed for conversation %s: %s", conversation_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail={"error": str(exc), "conversation_id": conversation_id},
        ) from exc

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


# Serve the chat UI (static, no build step) at /ui.
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
