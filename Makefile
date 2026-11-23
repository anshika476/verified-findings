.PHONY: test findings

# Runs every test offline (no network, no LLM).
test:
	python3 -m unittest discover -s tests -v

# Regenerates findings.json for the sample.
findings:
	python3 plearun_cli.py reports/report_fictional_02.txt records > findings.json
