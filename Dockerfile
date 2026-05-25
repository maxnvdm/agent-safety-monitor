# --- Frontend build stage ---
FROM node:20 AS frontend-build
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

# Python deps via pyproject.toml
COPY pyproject.toml ./
COPY monitor ./monitor
COPY api ./api
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# Copy built static frontend files to FastAPI/static (adjust if your backend serves static files differently)
COPY --from=frontend-build /app/frontend/dist ./frontend-dist

# Start command: Adjust as needed (uvicorn for FastAPI, for example)
EXPOSE 8070
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8070"]
