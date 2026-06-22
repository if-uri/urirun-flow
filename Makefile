.PHONY: help test conformance
help: ## Show commands
	@grep -E "^[a-zA-Z_-]+:.*?## .*$$" $(MAKEFILE_LIST) | awk "BEGIN{FS=\":.*?## \"}{printf \"  %-12s %s\\n\",\$$1,\$$2}"
test: ## Run the test suite (+ conformance)
	python -m pytest tests/ -q
conformance: ## Check every language emitter agrees with the Python reference
	python conformance.py
