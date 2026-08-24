.DEFAULT_GOAL := help
UV := uv

.PHONY: help install seed dev eval test lint fmt clean reset

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create venv and install deps (dev extras included)
	$(UV) sync --extra dev

seed: ## Generate the mock backend: SQLite data + Chroma KB
	$(UV) run python -m agent_ops.backend.seed

dev: ## Run the FastAPI service (http://127.0.0.1:8000, docs at /docs)
	$(UV) run uvicorn agent_ops.api.main:app --reload --host 127.0.0.1 --port 8000

eval: ## Run the eval harness over the golden dataset and print the report
	$(UV) run python -m agent_ops.eval.harness

test: ## Run the test suite
	$(UV) run pytest -q

lint: ## Lint with ruff
	$(UV) run ruff check src tests

fmt: ## Auto-format + fix with ruff
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

reset: ## Delete generated data (db, chroma, traces) and re-seed
	rm -rf data/aurora.db data/chroma data/traces eval_results
	$(MAKE) seed

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache build dist **/__pycache__
