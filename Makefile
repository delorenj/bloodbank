# Makefile for bloodbank event publisher v2.0

.PHONY: help install test test-cov test-quick test-watch lint format clean docs

help:  ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install project and test dependencies
	pip install -e .
	pip install -r tests/requirements-test.txt

test:  ## Run all tests with coverage
	pytest tests/ \
		--cov=. \
		--cov-report=html \
		--cov-report=term-missing \
		--cov-report=xml \
		--tb=short \
		-v

test-cov:  ## Run tests and open coverage report in browser
	pytest tests/ \
		--cov=. \
		--cov-report=html \
		--tb=short \
		-v
	@echo "Opening coverage report..."
	@command -v xdg-open >/dev/null 2>&1 && xdg-open htmlcov/index.html || \
	command -v open >/dev/null 2>&1 && open htmlcov/index.html || \
	echo "Please open htmlcov/index.html manually"

test-quick:  ## Run tests without coverage (fast)
	pytest tests/ -v --tb=short

test-watch:  ## Run tests in watch mode (requires pytest-watch)
	ptw tests/ -- -v --tb=short

test-parallel:  ## Run tests in parallel (requires pytest-xdist)
	pytest tests/ -n auto -v

test-integration:  ## Run only integration tests
	pytest tests/ -m integration -v

test-unit:  ## Run only unit tests
	pytest tests/ -m unit -v

test-failed:  ## Re-run only failed tests
	pytest tests/ --lf -v

test-debug:  ## Run tests with detailed debugging output
	pytest tests/ -vv --tb=long --log-cli-level=DEBUG

lint:  ## Run code linting
	ruff check .
	mypy . --ignore-missing-imports

format:  ## Format code with black and ruff
	black .
	ruff check --fix .

clean:  ## Clean up generated files
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov
	rm -rf coverage.xml
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete

docs:  ## Generate documentation
	@echo "Documentation target not yet implemented"

check:  ## Run all quality checks (lint + test)
	$(MAKE) lint
	$(MAKE) test

ci:  ## Run CI pipeline locally
	$(MAKE) clean
	$(MAKE) install
	$(MAKE) lint
	$(MAKE) test

# Test specific modules
test-correlation:  ## Test correlation tracking module
	pytest tests/test_correlation_tracking.py::TestCorrelationTrackerInitialization -v
	pytest tests/test_correlation_tracking.py::TestDeterministicEventIDGeneration -v
	pytest tests/test_correlation_tracking.py::TestAddingCorrelations -v
	pytest tests/test_correlation_tracking.py::TestQueryingCorrelationChains -v

test-degradation:  ## Test graceful degradation
	pytest tests/test_correlation_tracking.py::TestGracefulDegradation -v

test-publisher:  ## Test publisher integration
	pytest tests/test_correlation_tracking.py::TestPublisherIntegration -v

test-debug:  ## Test debug endpoints
	pytest tests/test_correlation_tracking.py::TestDebugEndpoints -v

test-edge-cases:  ## Test edge cases and error handling
	pytest tests/test_correlation_tracking.py::TestEdgeCasesAndErrorHandling -v

# Coverage targets
coverage-report:  ## Generate and view coverage report
	coverage report -m
	coverage html
	@echo "Opening coverage report..."
	@command -v xdg-open >/dev/null 2>&1 && xdg-open htmlcov/index.html || \
	command -v open >/dev/null 2>&1 && open htmlcov/index.html || \
	echo "Please open htmlcov/index.html manually"

coverage-xml:  ## Generate XML coverage report (for CI)
	coverage xml

# Development helpers
dev-setup:  ## Set up development environment
	pip install -e .
	pip install -r tests/requirements-test.txt
	pip install pre-commit
	pre-commit install

pre-commit:  ## Run pre-commit hooks on all files
	pre-commit run --all-files

# Docker targets (if needed)
docker-test:  ## Run tests in Docker container
	docker-compose -f docker-compose.test.yml up --abort-on-container-exit

docker-clean:  ## Clean up Docker test containers
	docker-compose -f docker-compose.test.yml down -v
