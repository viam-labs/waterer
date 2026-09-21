.PHONY: setup lint format test package clean

setup:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -e .[dev]

lint:
	./.venv/bin/ruff check src tests
	./.venv/bin/black --check src tests

format:
	./.venv/bin/ruff check --fix src tests
	./.venv/bin/black src tests

test:
	./.venv/bin/pytest

package:
	tar -czf module.tar.gz src pyproject.toml run.sh meta.json README.md LICENSE

clean:
	rm -rf .venv build dist *.egg-info module.tar.gz
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
