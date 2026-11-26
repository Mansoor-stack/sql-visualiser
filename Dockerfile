# syntax=docker/dockerfile:1.7
# Build arguments let you use internal mirrors; see README "Company networks".
ARG NODE_IMAGE=node:22-alpine
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${NODE_IMAGE} AS web
ARG NPM_REGISTRY=https://registry.npmjs.org/
WORKDIR /src/web
COPY web/package*.json ./
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -f /run/secrets/ca_cert ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_cert; fi; \
    npm config set registry "$NPM_REGISTRY" && \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY backend/ backend/
COPY --from=web /src/backend/sqlviz/static backend/sqlviz/static
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -f /run/secrets/ca_cert ]; then export PIP_CERT=/run/secrets/ca_cert; fi; \
    pip install --no-cache-dir --index-url "$PIP_INDEX_URL" . && rm -rf /src
WORKDIR /app
RUN useradd --create-home visualizer
USER visualizer
EXPOSE 8080
# 0.0.0.0 is needed inside the container; publish the port on 127.0.0.1 only (see compose.yaml).
CMD ["sql-visualiser", "--host", "0.0.0.0", "--port", "8080"]

FROM runtime AS test
ARG PIP_INDEX_URL=https://pypi.org/simple
USER root
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -f /run/secrets/ca_cert ]; then export PIP_CERT=/run/secrets/ca_cert; fi; \
    pip install --no-cache-dir --index-url "$PIP_INDEX_URL" "pytest>=8,<9"
COPY tests/ tests/
USER visualizer
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
