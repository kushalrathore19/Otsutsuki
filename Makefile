.PHONY: setup run test clean lint typecheck

setup:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install openai textual pytest

run:
	@echo "Note: Use TARGET_REPO=/path/to/repo make run to operate on a different repository."
	TASK="$${TASK:-Create a file named hello.txt containing the word success}" ./.venv/bin/python src/main.py

test:
	@echo "Running pytest..."
	PYTHONPATH=src ./.venv/bin/pytest tests/ -v

clean:
	rm -f status.json OUTCOME.md TASK.md hello.txt
	rm -f /tmp/*.patch
	rm -rf __pycache__ src/__pycache__ tests/__pycache__

lint:
	@if command -v ruff >/dev/null 2>&1 || ./.venv/bin/ruff --version >/dev/null 2>&1; then \
		echo "Running ruff..."; \
		./.venv/bin/ruff check src tests || ruff check src tests; \
	else \
		echo "Ruff not found. Skipping linting."; \
	fi

typecheck:
	@if command -v mypy >/dev/null 2>&1 || ./.venv/bin/mypy --version >/dev/null 2>&1; then \
		echo "Running mypy..."; \
		./.venv/bin/mypy src tests || mypy src tests; \
	else \
		echo "Mypy not found. Skipping typechecking."; \
	fi
