FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml .
COPY brain/ brain/
COPY migrations/ migrations/

# Install dependencies (no macOS-only extras)
RUN uv pip install --system --no-cache -e .

ENV MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    EMBEDDER_BACKEND=ollama \
    OLLAMA_URL=http://ollama:11434 \
    OLLAMA_MODEL=nomic-embed-text

EXPOSE 8000

CMD ["python", "-m", "brain.server"]
