.PHONY: cov notices

# Run tests with coverage reporting (opt-in; not part of the default pytest run).
cov:
	python -m pytest --cov=app --cov-report=term-missing --cov-report=html:htmlcov

# Regenerate THIRD_PARTY_NOTICES.md. Run it with the Python environment the
# image or installer is built from (PYTHON=/path/to/python to pick one).
PYTHON ?= python
notices:
	$(PYTHON) scripts/generate_third_party_notices.py
