#!/usr/bin/env python3
"""Summarize conversations into short titles for the sidebar.

Designed to be run periodically (e.g. from cron, every minute). The trigger
logic inside summarize_conversations decides which conversations are actually
due — those with enough messages or that have gone idle — so running it often
is cheap and safe.

    * * * * * cd /Users/ignacio/007 && .venv/bin/python summarize.py >> logs/summarize.log 2>&1
"""

import logging

import main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("summarize")


def run() -> int:
    titled = main.summarize_conversations(
        main.get_messages_collection(),
        main.get_conversations_collection(),
    )
    for cid, title in titled:
        logger.info("titled %s -> %r", cid, title)
    logger.info("summarized %d conversation(s)", len(titled))
    return len(titled)


if __name__ == "__main__":
    run()
