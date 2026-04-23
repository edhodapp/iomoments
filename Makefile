# iomoments top-level Makefile.
#
# Python-only today. Grows C and BPF targets in the same commit that adds
# the first C translation unit (per DECISIONS.md D008).

VENV            := .venv
VENV_STAMP      := $(VENV)/.installed
PY              := $(VENV)/bin/python
PYTEST          := $(VENV)/bin/pytest
MYPY            := $(VENV)/bin/mypy
PYLINT          := $(VENV)/bin/pylint
FLAKE8          := $(VENV)/bin/flake8
# Repo-committed pylintrc is the sole authority. Matches CI. No fallback
# to $(HOME)/.claude/pylintrc: a silent local pass while CI fails on the
# same commit defeats the purpose of committing the rcfile in the first
# place.
PYLINTRC        := pylintrc

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

# Stamp file tracks the last successful install. Rebuilds whenever
# pyproject.toml changes so dep additions take effect without requiring
# `make distclean && make venv` first.
$(VENV_STAMP): pyproject.toml
	@if [ ! -d "$(VENV)" ]; then python3 -m venv $(VENV); fi
	$(VENV)/bin/pip install --upgrade pip wheel >/dev/null
	$(VENV)/bin/pip install -e '.[dev]'
	@touch $(VENV_STAMP)

venv: $(VENV_STAMP)

install-hooks:
	tooling/hooks/install.sh

test: $(VENV_STAMP)
	$(PYTEST)

lint: lint-python

lint-python: $(VENV_STAMP)
	@if [ ! -f "$(PYLINTRC)" ]; then \
		echo "ERROR: $(PYLINTRC) missing — refusing to lint without it." >&2; \
		exit 1; \
	fi
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
