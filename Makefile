.PHONY: setup run test clean

setup:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install openai textual

run:
	./.venv/bin/python src/main.py

test:
	@echo "Test suite not yet implemented"

clean:
	rm -f status.json OUTCOME.md TASK.md
	rm -f /tmp/*.patch
	rm -rf __pycache__ src/__pycache__
