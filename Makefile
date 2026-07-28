.PHONY: install dev run

VENV := .venv
PY := $(VENV)/bin/python
PORT ?= 8000

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

dev:
	$(VENV)/bin/uvicorn main:app --reload --port $(PORT)

run:
	$(VENV)/bin/uvicorn main:app --port $(PORT)
