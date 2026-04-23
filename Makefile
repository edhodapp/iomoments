# iomoments top-level Makefile.
#
# Python-only today. Grows C and BPF targets in the same commit that adds
# the first C translation unit (per DECISIONS.md D008).

VENV            := .venv
PY              := $(VENV)/bin/python
PYTEST          := $(VENV)/bin/pytest
MYPY            := $(VENV)/bin/mypy
PYLINT          := $(VENV)/bin/pylint
FLAKE8          := $(VENV)/bin/flake8
PYLINTRC        := $(HOME)/.claude/pylintrc

PY_SOURCES      := $(shell find tests -type f -name '*.py' 2>/dev/null)

.PHONY: help venv install-hooks test lint lint-python fmt-check clean \
        gate-local distclean

help:
	@echo "iomoments make targets:"
	@echo "  venv           Create .venv and install dev deps."
	@echo "  install-hooks  Symlink project hooks into .git/hooks/."
	@echo "  test           Run pytest with branch coverage."
	@echo "  lint           Run all lint targets (today: lint-python only)."
	@echo "  lint-python    flake8 + pylint + mypy --strict on $(PY_SOURCES)."
	@echo "  fmt-check      Placeholder; clang-format check lands with first C."
	@echo "  gate-local     Full pre-push check: shellcheck + pytest + lint."
	@echo "  clean          Remove caches."
	@echo "  distclean      clean + remove .venv."

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip wheel >/dev/null
	$(VENV)/bin/pip install -e '.[dev]'

venv: $(VENV)/bin/python

install-hooks:
	tooling/hooks/install.sh

test: venv
	$(PYTEST)

lint: lint-python

lint-python: venv
	@if [ -z "$(PY_SOURCES)" ]; then \
		echo "(no Python sources to lint.)"; \
		exit 0; \
	fi
	$(FLAKE8) $(PY_SOURCES)
	$(PYLINT) --rcfile=$(PYLINTRC) $(PY_SOURCES)
	$(MYPY) --strict $(PY_SOURCES)

fmt-check:
	@echo "No .clang-format yet; fmt-check is a no-op until first C source (D008)."

# Replicates what pre-push.sh runs, minus the git-hook plumbing. Useful
# for "is my branch push-ready?" without actually pushing.
gate-local: lint test
	@if command -v shellcheck >/dev/null 2>&1; then \
		find tooling -type f -name '*.sh' -print0 \
		  | xargs -0 -r shellcheck; \
	else \
		echo "SKIP: shellcheck not installed."; \
	fi

clean:
	rm -rf .mypy_cache .pytest_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)
