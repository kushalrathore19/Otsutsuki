.PHONY: setup run test clean

setup:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install openai textual pytest

run:
	TASK="$${TASK:-Create a file named hello.txt containing the word success}" ./.venv/bin/python src/main.py

test:
	@echo "Running pytest..."
	PYTHONPATH=src ./.venv/bin/pytest tests/ -v

clean:
	rm -f status.json OUTCOME.md TASK.md
	rm -f /tmp/*.patch
	rm -rf __pycache__ src/__pycache__ tests/__pycache__
