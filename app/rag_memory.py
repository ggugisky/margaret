from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from lightrag import LightRAG, QueryParam
    from lightrag.llm.openai import gpt_4o_mini_complete, openai_embed
    from lightrag.utils import EmbeddingFunc

    _LIGHTRAG_AVAILABLE = True
except ImportError:
    class QueryParam:  # type: ignore[no-redef]
        def __init__(self, mode: str = "hybrid") -> None:
            self.mode = mode

    class EmbeddingFunc:  # type: ignore[no-redef]
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    LightRAG = None  # type: ignore[assignment]
    gpt_4o_mini_complete = None  # type: ignore[assignment]
    openai_embed = None  # type: ignore[assignment]
    _LIGHTRAG_AVAILABLE = False


class RagMemory:
    """Semantic long-term memory layer backed by LightRAG (RAG-Anything foundation).

    Runs alongside the SQLite Store — the Store owns exact/ordered retrieval,
    this owns cross-session semantic search.
    """

    def __init__(self, working_dir: str) -> None:
        if not _LIGHTRAG_AVAILABLE:
            raise RuntimeError(
                "lightrag-hku is not installed. Run: uv add lightrag-hku"
            )
        Path(working_dir).expanduser().mkdir(parents=True, exist_ok=True)
        self._rag = LightRAG(
            working_dir=working_dir,
            llm_model_func=gpt_4o_mini_complete,
            embedding_func=EmbeddingFunc(
                embedding_dim=1536,
                max_token_size=8192,
                func=openai_embed,
            ),
        )

    async def index_event(self, session_id: str, role: str, content: str) -> None:
        if role == "error" or not content.strip():
            return
        text = f"[session:{session_id}][{role}] {content}"
        try:
            await self._rag.ainsert(text)
        except Exception:
            logger.exception("rag index failed for session %s", session_id)

    async def search(self, query: str, mode: str = "hybrid") -> str:
        try:
            return await self._rag.aquery(query, param=QueryParam(mode=mode))
        except Exception:
            logger.exception("rag search failed for query %r", query)
            return ""

    async def build_context(self, query: str, mode: str = "hybrid") -> str:
        """Return a formatted memory context block to prepend to agent input.

        Returns empty string when no relevant memory is found.
        """
        result = await self.search(query, mode=mode)
        if not result or not result.strip():
            return ""
        return f"[관련 과거 기억]\n{result.strip()}\n---\n"
