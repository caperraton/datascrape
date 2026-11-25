.PHONY: install test lint format pre-commit docker-build docker-list clean

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest -q

lint:
	ruff check .
	bandit -r datascrape

format:
	ruff format .

pre-commit:
	pre-commit run --all-files

docker-build:
	docker compose build

docker-list:
	docker compose run --rm datascrape list-reports

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
