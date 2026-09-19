PYTHON ?= python3

.PHONY: check test validate normalize

check: test validate

test:
	$(PYTHON) -m unittest discover -s tests -q

validate:
	$(PYTHON) validate.py
	@if [ -f data/hansard/normalized/v1/SNAPSHOT.json ]; then \
		$(PYTHON) normalize_hansard.py validate; \
	fi

normalize:
	$(PYTHON) normalize_hansard.py normalize --all
	$(PYTHON) normalize_hansard.py validate
	$(PYTHON) build_second_readings_index.py
	$(PYTHON) validate.py
	$(PYTHON) normalize_hansard.py export
