# Entry points for humans and CI. Run `make help`.
SHELL := /bin/bash
SCRIPTS := host/lab container/bin/lab-supervisor container/bin/lab-kill-switch container/bin/lab-shell container/share/lab/bashrc $(wildcard tests/*.sh)
VPN ?= starting

.PHONY: help build up down status lint test test-target clean app-dev app-test app-build app-shots

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

test: lint ## lint + all tests that need no target machine (lab must be connected)
	tests/supervisor-test.sh $(VPN)
	tests/scope-test.sh
	tests/shell-log-test.sh
	tests/session-drop-test.sh

test-target: ## kill switch + proxy tests against a spawned machine: make test-target TARGET=10.129.x.x
	@test -n "$(TARGET)" || { echo "usage: make test-target TARGET=<ip of a machine you spawned>"; exit 1; }
	host/lab target $(TARGET)
	tests/target-test.sh $(TARGET) $(or $(PORT),80)
	tests/kill-switch-test.sh $(TARGET) $(or $(PORT),80)

clean: ## remove the container (workspace data is kept)
	-docker rm -f kali-lab

CARGO_PATH := PATH="$(HOME)/.cargo/bin:$$PATH"

app-dev: ## run the desktop app in development mode (hot reload)
	cd app && npm install --silent && $(CARGO_PATH) npm run tauri dev

app-test: ## type-check the UI, run Rust unit tests (no Docker needed)
	cd app && npm install --silent && npm run build
	cd app/src-tauri && $(CARGO_PATH) cargo test

app-build: ## build the release .app / .dmg
	cd app && npm install --silent && $(CARGO_PATH) npm run tauri build

app-shots: ## screenshots of every UI state (light + dark) into app/ui-shots/ (needs `make app-dev` or `npm run dev`)
	cd app && mkdir -p ui-shots && node scripts/ui-shots.mjs ui-shots light && node scripts/ui-shots.mjs ui-shots dark
