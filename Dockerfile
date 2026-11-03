# --- Frontend build stage ---
FROM node:22 AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN npm install -g pnpm@10.22.0
RUN pnpm install --frozen-lockfile
COPY frontend ./
RUN pnpm build

# --- Backend / Python stage ---
FROM python:3.11-slim AS backend
WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# Python deps from the committed lockfile (reproducible; matches CI)
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
# Dependencies first so this layer is cached across source-only changes
RUN uv sync --locked --no-dev --no-install-project
COPY monitor ./monitor
COPY api ./api
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"

# Copy built static frontend files to FastAPI/static (adjust if your backend serves static files differently)
COPY --from=frontend-build /app/frontend/dist ./frontend-dist

# Start command: Adjust as needed (uvicorn for FastAPI, for example)
EXPOSE 8070
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8070"]
