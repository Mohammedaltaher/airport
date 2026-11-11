"""LlamaIndex embedding adapter for the application's existing Google GenAI SDK."""
import asyncio

from google import genai
from google.genai import types
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from .config import DEFAULT_EMBED_BATCH_SIZE


class GeminiPolicyEmbedding(BaseEmbedding):
    _api_key: str = PrivateAttr()

    def __init__(self, api_key: str, model_name: str = 'gemini-embedding-001', embed_batch_size: int = DEFAULT_EMBED_BATCH_SIZE):
        super().__init__(model_name=model_name, embed_batch_size=embed_batch_size)
        self._api_key = api_key

    def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        # The key stays outside the serializable LlamaIndex model and persisted index.
        timeout = 45000 if task == 'RETRIEVAL_DOCUMENT' else 15000
        with genai.Client(api_key=self._api_key, http_options=types.HttpOptions(timeout=timeout)) as client:
            result = client.models.embed_content(
                model=self.model_name, contents=texts,
                config=types.EmbedContentConfig(task_type=task),
            )
        if not result.embeddings or len(result.embeddings) != len(texts):
            raise RuntimeError('Embedding provider returned an incomplete batch')
        vectors = [item.values for item in result.embeddings]
        if any(not vector for vector in vectors):
            raise RuntimeError('Embedding provider returned an empty vector')
        return vectors

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed([query], 'RETRIEVAL_QUERY')[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await asyncio.to_thread(self._get_query_embedding, query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embeddings([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, 'RETRIEVAL_DOCUMENT')
