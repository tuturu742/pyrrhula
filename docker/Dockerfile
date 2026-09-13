# One image. Different entrypoints. Config, not forks. (plan §13.8)
#
#   podman build -t pyrrhula:dev -f docker/Dockerfile .
#   podman run pyrrhula:dev api        # FastAPI server (uvicorn)
#   podman run pyrrhula:dev worker     # background worker (ingestion, embedding, jobs)
#   podman run pyrrhula:dev migrate    # one-shot: run Alembic migrations, then exit

FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv==0.11.29

WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY packages ./packages
COPY migrations ./migrations

# Sync into an in-project venv so the runtime stage can copy it verbatim.
RUN uv sync --frozen --no-dev --no-editable || uv sync --no-dev --no-editable

FROM python:3.12-slim AS runtime

# git: the server-side VCS store for delegated coding work (D15) runs real branch/commit/PR
# operations against volume-backed repos via `git` (packages/adapters/mcp/git_store.py). The
# hosted-service model keeps all branching/editing server-side -- never on a client checkout.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 pyrrhula
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY packages ./packages
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
# Workflow packs come from the pinned plugin repos (deploy/plugins.json); run
# scripts/fetch_plugins.py before building. In-image path stays /app/packs.
COPY builtin-workflows ./builtin-workflows
COPY .plugins/default ./packs
# The pin file: ensure_default_synced records the real provenance (url + sha), not
# the baked fallback.
COPY deploy/plugins.json ./deploy/plugins.json
COPY docker/entrypoint.sh /entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages \
    TIKTOKEN_CACHE_DIR=/app/.tiktoken_cache \
    HF_HOME=/app/.cache/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# A1.2: pre-create the BlobStore root so its ownership is baked into the image --
# named-volume mounts (docker/compose.selfhost.yml) initialize from the image's existing
# directory content/permissions, so this is what makes the volume writable by the
# non-root `pyrrhula` user instead of landing root-owned on first mount.
#
# Also pre-fetch tiktoken's cl100k_base encoding here, at build time (this stage still
# has network access) -- LiteLLMModelProvider.count_tokens falls back to it for any
# non-OpenAI model (Ollama/Anthropic/Gemini/echo-tokenizer-in-worker), and without a
# warm cache tiktoken tries to download it from openaipublic.blob.core.windows.net on
# first use. That silently breaks token counting (usage_record, A1.2 chunk sizing) the
# moment it runs in the actually-offline worker/api containers -- full-local is a
# supported deployment mode (plan §13.9), not just the model calls within it.
# HF_HOME points the local EmbeddingProvider (sentence-transformers, BAAI/bge-m3, 1024-dim)
# at the mounted HuggingFace cache volume (docker/compose.selfhost.yml: pyrrhula-hf ->
# /app/.cache/huggingface), and HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE make it load *purely*
# from that cache -- never phoning HuggingFace on first use. Without HF_HOME the library
# defaults to $HOME/.cache/huggingface (container-local, lost on every recreate), so the
# ~2.2GB model re-downloads on each new container and, in the actually-offline api/worker
# containers, that fails -- silently breaking context assembly (every generate turn embeds).
# The model is too large to bake into the image; it must be seeded into the volume once,
# out-of-band: `HF_HOME=<volume> huggingface-cli download BAAI/bge-m3` (or an equivalent
# snapshot_download run inside this image on a network-connected run). Pre-create the dir so
# a fresh named-volume mount initializes writable by the non-root user, same as blobs above.
# /app/plugins-local is the plugin drop directory (PYRRHULA_PLUGIN_DROP_DIR): packs an
# operator hand-places when this deployment cannot reach the pinned plugin repos. It
# exists in the image so a volume/ConfigMap can mount over it -- and so `kubectl cp` or
# `podman cp` has somewhere to land without the container running as root.
RUN mkdir -p /app/data/blobs /app/.tiktoken_cache /app/.cache/huggingface /app/plugins-local \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chmod +x /entrypoint.sh && chown -R pyrrhula:pyrrhula /app
USER pyrrhula

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
