"""v1 self-hosted EmbeddingProvider: bge-m3 (1024-dim) via sentence-transformers, CPU by
default (torch picks up a GPU automatically if one is visible — "GPU optional, CPU
workable"). ``sentence_transformers``/``torch`` are imported lazily, inside
``_load()``, not at module import time — importing this module (or constructing the
adapter) never requires the ~2GB of model weights to already be downloaded/cached; only
the first real ``.embed()`` call does. Mirrors ``LiteLLMModelProvider``'s lazy
``import litellm``/``import tiktoken`` pattern.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.ports.embedding import EmbedRequest, check_egress

# Peak memory of a transformer forward pass scales with batch_size * seq_len^2 (attention
# is quadratic in sequence length). bge-m3 is an XLM-R-large with a *default*
# max_seq_length of 8192, so handing `.encode()` an unbounded list of unbounded texts lets
# a single call size itself off the caller's data -- which OOM-killed the worker container
# (exit 137, 8Gi limit) the first time a real source-code corpus was ingested, taking every
# other tenant's queued job down with it, since the worker claims across tenants. Both
# bounds are therefore adapter-level resource safety, not tuning knobs: they are deliberately
# not settings, because no tenant benefits from a *different* value -- they benefit from the
# worker staying alive.
_DEFAULT_ENCODE_BATCH_SIZE = 8
# Comfortably above the ingestion chunker's ~400-token target, so bounded chunks are never
# truncated; for anything oversized that predates that bound, truncation is the intended
# failsafe -- a degraded vector beats a dead worker.
_DEFAULT_MAX_SEQ_LENGTH = 1024


class ModelNotDownloadedError(RuntimeError):
    """The model is not on this box, and this is not the moment to fetch it.

    A lazy in-request download is how the installer used to avoid a decision, and it
    cost more than it saved: minutes of stall inside whichever turn happened to be
    first, and -- observed live on Kubernetes -- an unauthenticated hub check with no
    timeout wedging the api's event loop for thirteen minutes at idle CPU. So the
    runtime never reaches the network on its own. Fetching is an explicit act from the
    admin console (a worker job, restartable, visible), and until it happens this says
    so in a sentence an operator can act on.
    """


def _require_cached(model: str) -> None:
    """Refuse to load a model that is not in the shared cache.

    Checked here rather than left to ``HF_HUB_OFFLINE``, because that env var produces
    a stack trace about a missing repo snapshot, which says nothing about what to do.
    """
    from core.retrieval_cache import presence

    if presence(model).present:
        return
    raise ModelNotDownloadedError(
        f"the embedding model {model!r} is not on this deployment. "
        "Admin console -> Models: choose the models you want and press Download "
        "(or upload a cache tarball if this box has no route to Hugging Face). "
        "Knowledge still ingests and chunks meanwhile; only semantic search waits."
    )


class SentenceTransformersEmbeddingProvider:
    def __init__(
        self,
        model: str = "BAAI/bge-m3",
        *,
        dimension: int = 1024,
        batch_size: int = _DEFAULT_ENCODE_BATCH_SIZE,
        max_seq_length: int = _DEFAULT_MAX_SEQ_LENGTH,
    ) -> None:
        self._model_name = f"local/{model}"
        self._hf_model_name = model
        self._dimension = dimension
        self._batch_size = batch_size
        self._max_seq_length = max_seq_length
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            _require_cached(self._hf_model_name)
            model = SentenceTransformer(self._hf_model_name)
            # Capped rather than left at the checkpoint's default: see the module constants.
            model.max_seq_length = min(
                self._max_seq_length, getattr(model, "max_seq_length", self._max_seq_length)
            )
            self._model = model
        return self._model

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        check_egress(req.purpose, req.model, req.egress_policy)
        model = self._load()
        vectors = await asyncio.to_thread(
            model.encode,
            list(req.texts),
            normalize_embeddings=True,
            batch_size=self._batch_size,
        )
        return [vector.tolist() for vector in vectors]
