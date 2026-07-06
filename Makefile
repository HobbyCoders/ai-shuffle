.PHONY: cov

# Run tests with coverage reporting (opt-in; not part of the default pytest run).
cov:
	python -m pytest --cov=app --cov-report=term-missing --cov-report=html:htmlcov
