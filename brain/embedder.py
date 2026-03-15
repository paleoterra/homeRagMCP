"""
Embedding engine — Ollama (default), MLX, or Apple NLEmbedding backends.

Backend is selected by the EMBEDDER_BACKEND environment variable:
  EMBEDDER_BACKEND=ollama       (default) — requires Ollama running locally
  EMBEDDER_BACKEND=mlx          — in-process, Apple Silicon only, no server needed
  EMBEDDER_BACKEND=nlembedding  — Apple built-in, zero download, macOS only, 512-dim
  EMBEDDER_BACKEND=openai       — OpenAI API, requires OPENAI_API_KEY, not local

All backends expose the same async embed() / embed_batch() interface so the
rest of the codebase is unaffected by whichever backend is chosen.

Recommended models:
  Ollama:       nomic-embed-text                    (768-dim, set via OLLAMA_MODEL)
  MLX:          mlx-community/nomic-embed-text-v1.5 (768-dim, set via MLX_MODEL)
  OpenAI:       text-embedding-3-small              (768-dim via dimensions param)
  NLEmbedding:  built-in, no model selection        (512-dim, fixed)

IMPORTANT: the vector dimension must match the database schema. Ollama, MLX, and
OpenAI all use 768-dim (migrations/001_initial.sql). NLEmbedding uses 512-dim and
requires a separate database initialised with migrations/001_initial_512.sql.
Never mix backends within the same database.
"""

import asyncio
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class _Backend(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Default: embed sequentially. MLX backend overrides with batching."""
        return [await self.embed(t) for t in texts]


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------


class _OllamaBackend(_Backend):
    def __init__(self, base_url: str, model: str) -> None:
        import ollama
        self.client = ollama.AsyncClient(host=base_url)
        self.model = model

    async def embed(self, text: str) -> list[float]:
        response = await self.client.embeddings(model=self.model, prompt=text)
        return response["embedding"]


# ---------------------------------------------------------------------------
# MLX backend
# ---------------------------------------------------------------------------


class _MLXBackend(_Backend):
    def __init__(self, model: str) -> None:
        try:
            from mlx_embeddings.utils import load
        except ImportError:
            raise ImportError(
                "mlx-embeddings is required for the MLX backend:\n"
                "  uv add mlx-embeddings\n"
                "Apple Silicon (M1+) required."
            )
        self._model, self._processor = load(model)

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_batch_sync, [text])

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Pass the full batch to MLX in one call — more efficient than sequential."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_batch_sync, texts)

    def _embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        from mlx_embeddings.utils import generate
        result = generate(self._model, self._processor, texts)
        return result.tolist()


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------


class _OpenAIBackend(_Backend):
    """OpenAI embedding API (text-embedding-3-small or text-embedding-3-large).

    Requires an OPENAI_API_KEY environment variable.
    Uses the dimensions parameter to request 768-dim output so vectors are
    compatible with the default schema without any migration.
    Not fully local — calls the OpenAI API over the internet.
    """

    def __init__(self, model: str, dimensions: int, api_key: str) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("openai is required for the OpenAI backend: uv add openai")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        response = await self.client.embeddings.create(
            input=text,
            model=self.model,
            dimensions=self.dimensions,
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """OpenAI accepts a list of strings in one API call."""
        response = await self.client.embeddings.create(
            input=texts,
            model=self.model,
            dimensions=self.dimensions,
        )
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


# ---------------------------------------------------------------------------
# Apple NLEmbedding backend
# ---------------------------------------------------------------------------


class _NLEmbeddingBackend(_Backend):
    """Apple's built-in sentence embeddings via the Natural Language framework.

    Zero download — model is part of macOS. Runs on Neural Engine.
    Produces 512-dimensional vectors, which requires a separate database
    schema (migrations/001_initial_512.sql) rather than the default 768-dim one.
    macOS only. Requires pyobjc-framework-naturallanguage.
    """

    def __init__(self) -> None:
        try:
            from NaturalLanguage import NLEmbedding, NLLanguageEnglish
        except ImportError:
            raise ImportError(
                "pyobjc-framework-naturallanguage is required for the NLEmbedding backend:\n"
                "  uv add pyobjc-framework-naturallanguage\n"
                "macOS only."
            )
        self._embedding = NLEmbedding.sentenceEmbeddingForLanguage_(NLLanguageEnglish)
        if self._embedding is None:
            raise RuntimeError(
                "NLEmbedding sentence model unavailable on this system. "
                "Requires macOS 12+ with language models installed."
            )

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_sync, text)

    def _embed_sync(self, text: str) -> list[float]:
        vec = self._embedding.vectorForString_(text)
        if vec is None:
            raise ValueError(f"NLEmbedding returned None for text: {text[:50]!r}")
        return list(vec)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class Embedder:
    def __init__(
        self,
        backend: str = "ollama",
        # Ollama options
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        # MLX options
        mlx_model: str = "mlx-community/nomic-embed-text-v1.5",
        # OpenAI options
        openai_model: str = "text-embedding-3-small",
        openai_dimensions: int = 768,
        openai_api_key: str = "",
    ) -> None:
        if backend == "mlx":
            self._backend = _MLXBackend(mlx_model)
        elif backend == "nlembedding":
            self._backend = _NLEmbeddingBackend()
        elif backend == "openai":
            self._backend = _OpenAIBackend(openai_model, openai_dimensions, openai_api_key)
        else:
            self._backend = _OllamaBackend(base_url, model)

    async def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns a float vector."""
        text = text.strip()
        if not text:
            raise ValueError("Cannot embed empty text")
        return await self._backend.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple strings. MLX batches them in one forward pass."""
        texts = [t.strip() for t in texts if t.strip()]
        return await self._backend.embed_batch(texts)
