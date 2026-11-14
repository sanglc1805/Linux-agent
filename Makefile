PYTHON ?= .venv/bin/python

.PHONY: help test check tools read-only-demo lab-demo

help:
	@echo "make test        run the full unittest suite"
	@echo "make check       syntax-check Python and shell files"
	@echo "make tools       list MCP tools (no API key needed)"
	@echo "make lab-demo    isolated write demo in /tmp (no root, no API writes to the host)"

test:
	$(PYTHON) -m unittest discover -v

check:
	$(PYTHON) -m compileall -q linux_agent server.py agent.py
	bash -n scripts/*.sh

tools:
	$(PYTHON) agent.py --list-tools

read-only-demo:
	$(PYTHON) agent.py --prompt "Give me a short health summary: host, uptime, disk usage and whether nginx is running. Use the tools."

lab-demo:
	bash scripts/lab_demo.sh
