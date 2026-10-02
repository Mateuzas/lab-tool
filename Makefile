# Entry points for humans and CI. Run `make help`.
SHELL := /bin/bash
SCRIPTS := host/lab container/bin/lab-supervisor container/bin/lab-kill-switch container/bin/lab-shell container/share/lab/bashrc $(wildcard tests/*.sh)
VPN ?= starting

.PHONY: help build up down status lint test test-target clean

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

build: ## build the lab image (recreates the container if it exists)
	host/lab build

up: ## connect, e.g. make up VPN=machines
	host/lab up $(VPN)

down: ## disconnect (egress stays blocked)
	host/lab off

status: ## show lab status
	host/lab status

lint: ## shellcheck every script (no local install needed)
	docker run --rm -v "$(CURDIR)":/mnt:ro -w /mnt koalaman/shellcheck:stable -S warning $(SCRIPTS)

test: lint ## lint + supervisor fault-injection test (no target machine needed)
	tests/supervisor-test.sh $(VPN)

test-target: ## kill switch + proxy tests against a spawned machine: make test-target TARGET=10.129.x.x
	@test -n "$(TARGET)" || { echo "usage: make test-target TARGET=<ip of a machine you spawned>"; exit 1; }
	tests/target-test.sh $(TARGET) $(or $(PORT),80)
	tests/kill-switch-test.sh $(TARGET) $(or $(PORT),80)

clean: ## remove the container (workspace data is kept)
	-docker rm -f kali-lab
