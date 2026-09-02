PYTHON ?= python3

.PHONY: help test conformance doctor-build doctor-test doctor-health
help: ## Show commands
	@grep -E "^[a-zA-Z_-]+:.*?## .*$$" $(MAKEFILE_LIST) | awk "BEGIN{FS=\":.*?## \"}{printf \"  %-12s %s\\n\",\$$1,\$$2}"
test: doctor-test conformance ## Run the test suite (+ conformance)
conformance: ## Check every language emitter agrees with the Python reference
	$(PYTHON) conformance.py

doctor-build:
	$(PYTHON) -m pip install -e ".[test,run]"

doctor-test:
	$(PYTHON) -m pytest tests/ -q

doctor-health:
	$(PYTHON) -c "import urirun_flow"
