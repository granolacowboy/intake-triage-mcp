# syntax=docker/dockerfile:1
# Container image for the intake-triage-mcp server (stdio transport).
# Published to ghcr.io and registered in the official MCP Registry.
FROM python:3.12-slim

WORKDIR /app

# Runtime deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Server + bundled (fictional) sample data. server.py loads data/ relative to
# its own location, so they must sit together under /app.
COPY server.py .
COPY data ./data

# MCP Registry ownership verification for the OCI package type: this label MUST
# match the "name" field in server.json.
LABEL io.modelcontextprotocol.server.name="io.github.granolacowboy/intake-triage-mcp"

# stdio JSON-RPC on stdin/stdout; logs go to stderr.
ENTRYPOINT ["python", "server.py"]
