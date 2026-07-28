.PHONY: install dev run mongo mongo-stop init

VENV := .venv
PY := $(VENV)/bin/python
PORT ?= 8000
MONGO_PORT ?= 27017
MONGO_NAME ?= mongo-dev

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

dev:
	$(VENV)/bin/uvicorn main:app --reload --port $(PORT)

run:
	$(VENV)/bin/uvicorn main:app --port $(PORT)

mongo:
	docker run -d --name $(MONGO_NAME) -p $(MONGO_PORT):27017 mongo:latest

mongo-stop:
	docker rm -f $(MONGO_NAME)

init:
	MONGO_NAME=$(MONGO_NAME) ./init.sh
