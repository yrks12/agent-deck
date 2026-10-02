.PHONY: setup test test-backend test-macos check build-macos app

PYTHON ?= .venv/bin/python

setup:
	uv venv --python 3.13 .venv
	uv pip install --python $(PYTHON) -r requirements-dev.txt

test-backend:
	$(PYTHON) -m pytest -q

test-macos:
	swift test --package-path macos

test: test-backend test-macos

check:
	git diff --check
	$(PYTHON) -m pytest -q tests/test_monorepo_layout.py tests/test_repository_governance.py

app: build-macos

build-macos:
	macos/Scripts/make-app-bundle.sh release
