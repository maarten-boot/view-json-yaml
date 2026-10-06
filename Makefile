# view-json-yaml - a viewer for JSON and YAML.  `make help` lists the targets.
#
# Every target runs inside .venv, which is created on first use and kept up to date with
# pyproject.toml. Nothing is installed into the system interpreter; PYTHON is only used to
# create the virtual environment.

PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
RUFF := $(VENV)/bin/ruff
APP := $(VENV)/bin/view-json-yaml
STAMP := $(VENV)/.installed
FILE ?=

.DEFAULT_GOAL := help

help:  ## list these targets
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

# The stamp is remade whenever the dependency declarations change, so the venv cannot go stale.
$(STAMP): pyproject.toml requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet --editable ".[dev]"
	@touch $@

venv: $(STAMP)  ## create .venv and install the project, its dependencies and the dev tools
	@echo "ready: $(VENV)"

run: $(STAMP)  ## start the app, optionally on a file: make run FILE=examples/report.rl.json
	$(APP) $(if $(FILE),--file=$(FILE))

demo: $(STAMP)  ## open the sample OpenAPI spec
	$(APP) --file=examples/openapi.yaml

test: $(STAMP)  ## the whole suite
	$(PY) -m pytest

test-fast: $(STAMP)  ## only the tests that need no display
	$(PY) -m pytest -m "not gui"

test-gui: $(STAMP)  ## only the widget tests
	$(PY) -m pytest -m gui

test-headless: $(STAMP)  ## the whole suite under a virtual display
	xvfb-run -a $(PY) -m pytest

lint: $(STAMP)  ## report style problems
	$(RUFF) check .

format: $(STAMP)  ## rewrite the code the way ruff wants it
	$(RUFF) format .

check: $(STAMP)  ## before committing: formatting, lint, then the display-free tests
	$(RUFF) format --check .
	$(RUFF) check .
	$(PY) -m pytest -m "not gui"

build: $(STAMP)  ## build the wheel and the sdist into dist/ (hatchling does the work)
	rm -rf dist
	$(PY) -m build

publish-test: $(STAMP)  ## upload to TestPyPI first; install from there before the real thing
	$(PY) -m twine upload --repository mboot_testpypi dist/*

publish: $(STAMP)  ## upload to PyPI
	$(PY) -m twine check dist/*
	$(PY) -m twine upload --repository mboot_pypi dist/*

version: $(STAMP)  ## print the version the app reports
	@$(PY) -c "import view_json_yaml; print(view_json_yaml.__version__)"

clean:  ## remove caches and build output, keeping the venv
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean  ## also remove the virtual environment
	rm -rf $(VENV)

all: distclean lint format check build

.PHONY: help venv run demo test test-fast test-gui test-headless lint format check build \
	publish-test publish version clean distclean
