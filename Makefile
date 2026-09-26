.PHONY: setup run test clean

setup:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install openai textual

run:
	TASK="$${TASK:-Create a file named hello.txt containing the word success}" ./.venv/bin/python src/main.py

test:
	@echo "Running initialization smoke test..."
	AI_API_KEY=dummy_key_for_test ./.venv/bin/python -c "import sys; sys.path.insert(0, 'src'); from orchestrator import Orchestrator; Orchestrator(); print('Orchestrator initialized successfully')"
	@echo "Running unit tests..."
	./.venv/bin/python -m unittest discover -s . -p "test_*.py"

clean:
	rm -f status.json OUTCOME.md TASK.md
	rm -f /tmp/*.patch
	rm -rf __pycache__ src/__pycache__
