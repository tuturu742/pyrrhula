"""The api composition root for ``Reranker`` selection. ``reranker_enabled=False`` (Admin
→ Models) returns ``None`` — the degraded mode for tiny deployments; callers pass whatever
this returns straight into ``search_and_budget(reranker=...)``, which already treats
``None`` as "skip reranking."
"""

from __future__ import annotations

from adapters.reranker.local.provider import CrossEncoderReranker
from adapters.reranker.stub.provider import StubReranker
from core.deployment_settings import current_retrieval_models
from core.ports.reranker import Reranker

_reranker: Reranker | None = None
_reranker_initialized = False


def get_reranker() -> Reranker | None:
    global _reranker, _reranker_initialized
    models = current_retrieval_models()
    if not models.reranker_enabled:
        return None

    if not _reranker_initialized:
        model_name = models.reranker_model
        if model_name == "local/stub-reranker":
            _reranker = StubReranker()
        elif model_name.startswith("local/"):
            _reranker = CrossEncoderReranker(model_name.removeprefix("local/"))
        else:
            raise ValueError(f"no cloud reranker adapter yet; got {model_name!r}")
        _reranker_initialized = True
    return _reranker
