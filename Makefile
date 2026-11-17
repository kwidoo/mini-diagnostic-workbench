# MINI Diagnostic Workbench - exact commands for a clean Mac (needs: uv, Node 22+)
.PHONY: setup dev serve test test-backend test-frontend lint demo build fixtures docker-test docker-demo docker-serve

setup:            ## install pinned dependencies from the lockfiles
	cd backend && uv sync --frozen
	cd frontend && npm ci

build:            ## build the dashboard into frontend/dist (served by the backend)
	cd frontend && npm run build

serve: build      ## dashboard + API on http://127.0.0.1:8765 (local only)
	cd backend && uv run mini-obd serve

dev:              ## API on :8765 and Vite dev server on http://127.0.0.1:5173
	(cd backend && uv run mini-obd serve) & (cd frontend && npm run dev); kill %1 2>/dev/null || true

test: test-backend test-frontend

test-backend:
	cd backend && uv run ruff check app tests && uv run pytest

test-frontend:
	cd frontend && npm run typecheck && npm test

lint:
	cd backend && uv run ruff check app tests && uv run ruff format --check app tests

demo:             ## offline end-to-end demo against the fake ELM327 (synthetic data)
	cd backend && MINI_OBD_DB=$${TMPDIR:-/tmp}/mini-obd-demo.sqlite3 uv run mini-obd demo

fixtures:         ## regenerate the synthetic fixtures
	cd backend && uv run mini-obd fixtures

# Docker (optional): tests, replay and simulator. Real-adapter use from a container is untested.
docker-test:
	docker compose run --rm test && docker compose run --rm test-frontend

docker-demo:
	docker compose run --rm demo

docker-serve:
	docker compose up --build serve
