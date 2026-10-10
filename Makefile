# Everyday commands. Everything runs in the foreground: Ctrl+C stops it, nothing lingers.
# `make` or `make help` lists the targets.

UV  := uv run
FIN := $(UV) fin

.DEFAULT_GOAL := help
.PHONY: help serve open dev web weekly sync process review import rebuild backup doctor \
        test lint check stop screenshots

help: ## List the targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*## "}; {printf "  make %-10s %s\n", $$1, $$2}'

# --- dashboard ---------------------------------------------------------------

serve: web ## Dashboard on http://127.0.0.1:8765 (Ctrl+C to stop)
	$(FIN) serve

open: web ## Same, and open it in the browser
	$(FIN) serve --open

dev: ## Live-reload frontend on :5173 plus the API (Ctrl+C stops both)
	@trap 'kill 0' INT TERM EXIT; \
	  $(FIN) serve & \
	  npm --prefix web run dev

web: web/node_modules ## Build the dashboard if its sources changed
	@if [ ! -f web/dist/index.html ] || [ -n "$$(find web/src web/index.html -newer web/dist/index.html)" ]; \
	  then npm --prefix web run build; fi

web/node_modules: web/package.json
	npm --prefix web install
	@touch web/node_modules

# --- data --------------------------------------------------------------------

weekly: ## Sync every site (you log in), process, back up, open the dashboard
	$(FIN) weekly

sync: ## One site: make sync SITE=chase   (chase|citi|amex|amazon|costco)
	@test -n "$(SITE)" || (echo "usage: make sync SITE=chase" && exit 2)
	$(FIN) sync $(SITE)

process: ## Categorize, match, split, reconcile (add NO_AI=1 to skip AI)
	$(FIN) process $(if $(NO_AI),--no-ai,)

review: ## Open review items, in the terminal
	$(FIN) review

import: ## Import everything waiting in the inbox
	$(FIN) import --inbox

rebuild: ## Rebuild derived data from raw/ (keeps your corrections)
	$(FIN) rebuild

backup: ## SQLite backup into backups/
	$(FIN) backup

doctor: ## Setup and security checks
	$(FIN) doctor

stop: ## Stop a dashboard left running in the background, if any
	@pkill -f "fin serve" && echo "stopped" || echo "nothing running"

# --- development -------------------------------------------------------------

test: ## Python tests
	$(UV) pytest

lint: ## Ruff
	$(UV) ruff check .

check: lint test ## Lint, tests and a type-checked web build
	npm --prefix web run build

screenshots: web ## README screenshots from a throwaway mock database (headless Chrome)
	$(UV) python scripts/make_screenshots.py
