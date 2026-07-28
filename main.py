import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection

logger = logging.getLogger("chat")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(_handler)
    logger.propagate = False  # avoid double logging via the root logger

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "app")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "google/gemma-4-26b-a4b")
SUMMARY_INTERVAL_SECONDS = int(os.getenv("SUMMARY_INTERVAL_SECONDS", "60"))

_summary_stop = threading.Event()
_summary_thread: threading.Thread | None = None


def _summary_worker(stop: threading.Event, interval: int) -> None:
    """Periodically (re)title conversations until told to stop.

    Runs in a background daemon thread inside the server process. The work is
    synchronous (pymongo + httpx), so it stays off the async event loop. Any
    error in an iteration is logged and the loop continues.
    """
    logger.info("summary worker started (interval=%ss)", interval)
    while not stop.is_set():
        try:
            titled = summarize_conversations(
                get_messages_collection(), get_conversations_collection()
            )
            if titled:
                logger.info(
                    "summary worker titled %d conversation(s)", len(titled)
                )
        except Exception:
            logger.exception("summary worker iteration failed")
        stop.wait(interval)  # interruptible sleep
    logger.info("summary worker stopped")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _summary_thread
    if os.getenv("SUMMARY_WORKER", "1") == "1":
        _summary_stop.clear()
        _summary_thread = threading.Thread(
            target=_summary_worker,
            args=(_summary_stop, SUMMARY_INTERVAL_SECONDS),
            name="summary-worker",
            daemon=True,
        )
        _summary_thread.start()
    try:
        yield
    finally:
        _summary_stop.set()
        if _summary_thread is not None:
            _summary_thread.join(timeout=5)


app = FastAPI(title="Chat Server", lifespan=lifespan)

_client: MongoClient | None = None


def _db():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client[MONGO_DB]


def get_messages_collection() -> Collection:
    """FastAPI dependency: the `messages` collection. Overridable in tests."""
    return _db()["messages"]


def get_conversations_collection() -> Collection:
    """FastAPI dependency: the `conversations` collection (titles/summaries)."""
    return _db()["conversations"]


class LLMError(Exception):
    """Raised when LM Studio cannot produce a reply, with a readable reason."""


@dataclass
class LLMReply:
    """An assistant reply plus the model that actually generated it."""

    content: str
    model: str


def call_lmstudio(history: list[dict]) -> LLMReply:
    """Send the conversation history to LM Studio's OpenAI-compatible API.

    Returns the reply content and the model id LM Studio reports having used
    (which can differ from the requested one). Raises LLMError with a specific,
    logged reason on any failure so the cause is visible (not an opaque 500).
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
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(
            f"unexpected LM Studio response shape: {resp.text[:500]!r}"
        ) from exc
    # prefer the model LM Studio actually used; fall back to what we requested
    return LLMReply(content=content, model=data.get("model") or LMSTUDIO_MODEL)


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


# --- conversation summarization (title generation) ---------------------------

SUMMARY_MIN_MESSAGES = int(os.getenv("SUMMARY_MIN_MESSAGES", "4"))
SUMMARY_IDLE_SECONDS = int(os.getenv("SUMMARY_IDLE_SECONDS", "120"))

_TITLE_SYSTEM_PROMPT = (
    "You write very short conversation titles. Summarize the conversation as a "
    "concise title of at most 6 words. Reply with ONLY the title — no quotes, "
    "no trailing punctuation, no preamble."
)


def _clean_title(raw: str) -> str:
    """Normalize an LLM-produced title: single line, unquoted, bounded length."""
    title = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    title = title.strip("\"'").strip()
    return title[:80]


def generate_title(history: list[dict]) -> str:
    """Ask LM Studio for a short title summarizing the conversation."""
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    reply = call_lmstudio(
        [
            {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:4000]},
        ]
    )
    return _clean_title(reply.content)


def _naive(dt: datetime) -> datetime:
    """Drop tzinfo so aware/naive (Mongo returns naive UTC) can be compared."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def summarize_conversations(
    messages: Collection,
    conversations: Collection,
    titler=generate_title,
    *,
    now: datetime | None = None,
    min_messages: int = SUMMARY_MIN_MESSAGES,
    idle_seconds: int = SUMMARY_IDLE_SECONDS,
) -> list[tuple[str, str]]:
    """Generate/refresh titles for conversations that are due.

    A conversation is due when it has at least `min_messages` messages OR its
    last message is at least `idle_seconds` old, and it either has no title yet
    or has grown by `min_messages` since it was last summarized. Returns the
    list of (conversation_id, title) that were (re)titled.
    """
    now = now or datetime.now(timezone.utc)
    idle_cutoff = _naive(now) - timedelta(seconds=idle_seconds)
    summarized: list[tuple[str, str]] = []

    for cid in messages.distinct("conversation_id"):
        docs = list(
            messages.find({"conversation_id": cid}).sort("created_at", ASCENDING)
        )
        if not docs:
            continue
        count = len(docs)
        last_activity = _naive(docs[-1]["created_at"])

        triggered = count >= min_messages or last_activity <= idle_cutoff
        if not triggered:
            continue

        existing = conversations.find_one({"conversation_id": cid}) or {}
        already = existing.get("summarized_count", 0)
        due = not existing.get("title") or count >= already + min_messages
        if not due:
            continue

        history = [{"role": d["role"], "content": d["content"]} for d in docs]
        title = titler(history)
        conversations.update_one(
            {"conversation_id": cid},
            {
                "$set": {
                    "conversation_id": cid,
                    "title": title,
                    "summarized_count": count,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        summarized.append((cid, title))

    return summarized


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
        reply = llm(history)
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

    # store the assistant reply as its own document, tagged with the model used
    messages.insert_one(
        {
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": reply.content,
            "model": reply.model,
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"conversation_id": conversation_id, "reply": reply.content}


@app.get("/conversations")
def list_conversations(
    messages: Collection = Depends(get_messages_collection),
    conversations: Collection = Depends(get_conversations_collection),
):
    """List conversations (aggregated from messages), most recent first.

    Each item carries a summarized `title` when one has been generated by the
    summarization job (see summarize_conversations); `preview` is the raw first
    message as a fallback for display.
    """
    pipeline = [
        {"$sort": {"created_at": ASCENDING}},
        {
            "$group": {
                "_id": "$conversation_id",
                "message_count": {"$sum": 1},
                "created_at": {"$first": "$created_at"},
                "updated_at": {"$last": "$created_at"},
                "preview": {"$first": "$content"},
            }
        },
        {"$sort": {"updated_at": -1}},
    ]
    items = [
        {
            "conversation_id": doc["_id"],
            "message_count": doc["message_count"],
            "created_at": doc["created_at"],
            "updated_at": doc["updated_at"],
            "preview": (doc.get("preview") or "")[:80],
        }
        for doc in messages.aggregate(pipeline)
    ]
    titles = {
        c["conversation_id"]: c.get("title")
        for c in conversations.find(
            {"conversation_id": {"$in": [i["conversation_id"] for i in items]}}
        )
    }
    for item in items:
        item["title"] = titles.get(item["conversation_id"])
    return {"conversations": items}


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
                **({"model": d["model"]} if d.get("model") else {}),
            }
            for d in docs
        ],
    }


# Serve the chat UI (static, no build step) at /ui.
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
