# Single source of truth for project commands (documentation ergonomics, not an
# abstraction — Rule 16-safe). On Windows without `make`, run the commands directly;
# they are mirrored in README.md / CLAUDE.md. Run inside an activated .venv.

.PHONY: setup lint fmt typecheck test eval check help

help:  ## list targets
	@echo "setup lint fmt typecheck test eval check"

setup:      ## install runtime + dev/eval deps
	pip install -r requirements.txt -r requirements-dev.txt

lint:       ## ruff lint
	ruff check ingest

fmt:        ## ruff format (write)
	ruff format ingest

typecheck:  ## mypy
	mypy ingest

test:       ## pytest (smoke + unit)
	pytest

eval:       ## promptfoo PR sample (node CLI, not pip)
	npx promptfoo eval -c ingest/eval/promptfoo.yaml

check: lint typecheck test  ## lint + types + tests
