.PHONY: install install-dev dev run mongo mongo-stop init test test-e2e summarize

VENV := .venv
PY := $(VENV)/bin/python
PORT ?= 8000
MONGO_PORT ?= 27017
MONGO_NAME ?= mongo-dev
LOG_DIR ?= logs
LOG_FILE ?= $(LOG_DIR)/backend.log

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

install-dev:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements-dev.txt

test:
	$(VENV)/bin/pytest -q

test-e2e:
	$(VENV)/bin/pytest -q -m e2e

run:
	mkdir -p $(LOG_DIR)
	$(VENV)/bin/uvicorn main:app --reload --port $(PORT) 2>&1 | tee $(LOG_FILE)

mongo:
	docker run -d --name $(MONGO_NAME) -p $(MONGO_PORT):27017 mongo:latest

mongo-stop:
	docker rm -f $(MONGO_NAME)

init:
	MONGO_NAME=$(MONGO_NAME) ./init.sh

# The server already titles conversations via an in-process background thread
# (SUMMARY_INTERVAL_SECONDS, default 60s). This target runs one pass manually,
# e.g. for a one-off backfill or from cron if you disable the worker with
# SUMMARY_WORKER=0.
summarize:
	$(VENV)/bin/python summarize.py
