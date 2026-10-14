# syntax=docker/dockerfile:1
# Container image for the intake-triage-mcp server (stdio transport).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create the runtime identity before copying application files. Package
# installation happens as root during the image build; the server never does.
RUN groupadd --system mcp \
    && useradd --system --gid mcp --home-dir /app --no-create-home mcp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=mcp:mcp server.py .
COPY --chown=mcp:mcp data ./data

# MCP Registry ownership verification for the OCI package type.
LABEL io.modelcontextprotocol.server.name="io.github.granolacowboy/intake-triage-mcp"

# The default append-only log is /app/triage_log.jsonl, so the runtime user
# owns /app but has no root privileges.
RUN chown mcp:mcp /app
USER mcp

ENTRYPOINT ["python", "server.py"]
