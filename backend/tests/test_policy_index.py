from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
import csv

import pytest
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from dma.backend.app.embeddings import GeminiPolicyEmbedding
from dma.backend.app.config import Settings
from dma.backend.app.policies import PolicyRetrievalError, PolicyStore


class TestEmbedding(BaseEmbedding):
    """Deterministic semantic fixture; never used by the application."""
    __test__ = False
    _text_calls: int = PrivateAttr(default=0)
    _fail_after: int | None = PrivateAttr(default=None)

    def _get_text_embedding(self, text):
        if self._fail_after is not None and self._text_calls >= self._fail_after:
            raise RuntimeError('Synthetic batch failure')
        self._text_calls += 1
        return [1.0, 0.0] if 'Immigration' in text else [0.0, 1.0]

    def _get_query_embedding(self, query):
        return [1.0, 0.0] if 'الجوازات' in query or 'border' in query else [0.0, 1.0]

    async def _aget_query_embedding(self, query):
        return self._get_query_embedding(query)


@pytest.fixture
def documents(tmp_path):
    folder = tmp_path / 'documents'
    folder.mkdir()
    rows = [
        ['GIA-TEST-01', 'APPROVED', '1 January 2026', '1.0', 'current.txt'],
        ['GIA-TEST-02', 'APPROVED', '1 January 2027', '2.0', 'future.txt'],
        ['GIA-TEST-03', 'DRAFT', '1 January 2026', '3.0', 'draft.txt'],
    ]
    with (folder / 'Document_Register.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['doc_no', 'status', 'effective', 'version', 'file'])
        writer.writerows(rows)
    (folder / 'current.txt').write_text('1.1 Immigration queue wait limit is 15 minutes.\n1.2 Parking accepts card payments.', encoding='utf-8')
    # More invalid close matches than top-k verifies filtering happens before retrieval.
    (folder / 'future.txt').write_text('\n'.join(f'1.{i} Immigration future limit.' for i in range(20)), encoding='utf-8')
    (folder / 'draft.txt').write_text('1.1 Immigration draft limit.', encoding='utf-8')
    return folder


def test_vector_retrieval_ranks_semantic_match_and_prefilters_invalid_policies(documents):
    store = PolicyStore(documents, embed_model=TestEmbedding(model_name='test-v1'))
    found = store.search('ما مدة الانتظار عند الجوازات؟', date(2026, 9, 1), date(2026, 9, 30))
    assert len(found) == 2
    assert found[0]['section'] == '1.1'
    assert all(item['document'] == 'GIA-TEST-01' for item in found)
    assert all(node.metadata['status'] != 'DRAFT' for node in store.nodes)
    assert store.search('border queue', date(2025, 1, 1), date(2025, 12, 31)) == []


def test_index_cache_reuses_vectors_and_invalidates_on_source_or_model_change(documents, tmp_path):
    cache = tmp_path / 'index'
    first_model = TestEmbedding(model_name='test-v1')
    first = PolicyStore(documents, embed_model=first_model, index_path=cache)
    first.search('border queue', date(2026, 9, 1), date(2026, 9, 30))
    assert first_model._text_calls > 0
    second_model = TestEmbedding(model_name='test-v1')
    second = PolicyStore(documents, embed_model=second_model, index_path=cache)
    second.search('border queue', date(2026, 9, 1), date(2026, 9, 30))
    assert second_model._text_calls == 0
    assert second.index_path == first.index_path
    (documents / 'current.txt').write_text('1.1 Immigration revised limit.', encoding='utf-8')
    changed = PolicyStore(documents, embed_model=second_model, index_path=cache)
    assert changed.index_path != first.index_path
    new_model = PolicyStore(documents, embed_model=TestEmbedding(model_name='test-v2'), index_path=cache)
    assert new_model.index_path != changed.index_path


def test_embedding_failures_are_explicit(documents, monkeypatch):
    store = PolicyStore(documents, embed_model=TestEmbedding())
    monkeypatch.setattr(store, '_ensure_index', MagicMock(side_effect=RuntimeError('provider failure')))
    with pytest.raises(PolicyRetrievalError):
        store.search('border queue', date(2026, 9, 1), date(2026, 9, 30))


def test_interrupted_index_resumes_from_successful_batches(documents, tmp_path):
    first_model = TestEmbedding(model_name='test-v1', embed_batch_size=1)
    first_model._fail_after = 1
    first = PolicyStore(documents, embed_model=first_model, index_path=tmp_path / 'cache')
    with pytest.raises(PolicyRetrievalError):
        first.search('border queue', date(2026, 9, 1), date(2026, 9, 30))
    second_model = TestEmbedding(model_name='test-v1', embed_batch_size=1)
    second = PolicyStore(documents, embed_model=second_model, index_path=tmp_path / 'cache')
    assert second.search('border queue', date(2026, 9, 1), date(2026, 9, 30))
    assert second_model._text_calls == len(second.nodes) - 1


def test_google_adapter_uses_retrieval_tasks_and_keeps_key_out_of_model(monkeypatch):
    client = MagicMock()
    client.models.embed_content.return_value = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0])])
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    monkeypatch.setattr('dma.backend.app.embeddings.genai.Client', factory)
    embedding = GeminiPolicyEmbedding('test-secret')
    assert embedding.get_text_embedding('Policy clause') == [1.0, 0.0]
    assert client.models.embed_content.call_args.kwargs['config'].task_type == 'RETRIEVAL_DOCUMENT'
    assert embedding.get_query_embedding('Question') == [1.0, 0.0]
    assert client.models.embed_content.call_args.kwargs['config'].task_type == 'RETRIEVAL_QUERY'
    assert 'test-secret' not in embedding.model_dump_json()


def test_config_controls_chunking_retrieval_and_embedding_batches(documents, tmp_path, monkeypatch):
    for name, value in {'DMA_CHUNK_SIZE': '32', 'DMA_CHUNK_OVERLAP': '8',
                        'DMA_SIMILARITY_TOP_K': '1', 'DMA_EMBED_BATCH_SIZE': '4'}.items():
        monkeypatch.setenv(name, value)
    settings = Settings(documents=documents, policy_index=tmp_path / 'cache', api_key='test-key')
    wired = PolicyStore.from_settings(settings)
    assert wired.embed_model.embed_batch_size == 4
    assert wired.similarity_top_k == 1
    (documents / 'current.txt').write_text('1.1 Immigration ' + ' '.join(f'word{i}' for i in range(100)), encoding='utf-8')
    store = PolicyStore(documents, embed_model=TestEmbedding(), chunk_size=settings.chunk_size,
                        chunk_overlap=settings.chunk_overlap, similarity_top_k=settings.similarity_top_k)
    chunks = [node for node in store.nodes if node.metadata['document'] == 'GIA-TEST-01']
    assert len(chunks) > 1
    assert all(node.metadata['section'] == '1.1' for node in chunks)
    assert chunks[0].metadata['text'].split()[-1] in chunks[1].metadata['text'].split()[:8]
    assert len(store.search('border queue', date(2026, 9, 1), date(2026, 9, 30))) == 1
    changed = PolicyStore(documents, embed_model=TestEmbedding(), index_path=tmp_path / 'cache',
                          chunk_size=32, chunk_overlap=0)
    overlapping = PolicyStore(documents, embed_model=TestEmbedding(), index_path=tmp_path / 'cache',
                              chunk_size=32, chunk_overlap=8)
    assert changed.index_path != overlapping.index_path


@pytest.mark.parametrize('values', [
    {'chunk_size': 0}, {'chunk_overlap': -1}, {'chunk_size': 32, 'chunk_overlap': 32},
    {'similarity_top_k': 0}, {'embed_batch_size': 0}, {'embed_batch_size': 2049},
])
def test_invalid_retrieval_settings_fail_early(values):
    with pytest.raises(ValueError):
        Settings(**values)
