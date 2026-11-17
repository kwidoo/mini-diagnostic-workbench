# Optional container for tests, replay and the simulator. Local macOS execution is the primary path.
FROM node:22-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /srv/backend
ENV UV_LINK_MODE=copy MINI_OBD_DB=/data/workbench.sqlite3 MINI_OBD_FIXTURES=/srv/fixtures MINI_OBD_FRONTEND=/srv/frontend/dist
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-install-project
COPY backend/ ./
RUN uv sync --frozen
COPY fixtures/ /srv/fixtures/
COPY --from=frontend /src/frontend/dist /srv/frontend/dist
VOLUME /data
EXPOSE 8765
CMD ["uv", "run", "mini-obd", "serve", "--host", "0.0.0.0", "--port", "8765"]
