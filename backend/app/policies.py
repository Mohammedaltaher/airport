"""Explicit date-aware rules and clause retrieval from the provided register."""
from datetime import date, datetime
from pathlib import Path
from threading import Lock
import csv
import hashlib
import json
import re

from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import MetadataMode, TextNode
from llama_index.core.storage.kvstore import SimpleKVStore

from .config import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, DEFAULT_SIMILARITY_TOP_K, Settings
from .embeddings import GeminiPolicyEmbedding


def service_rule(day: date, hour: int) -> dict:
    current = day >= date(2026, 1, 1)
    return {
        'average_limit': 15 if current else 20,
        'tail_limit': 5 if current else 10,
        'peak': (17 if current else 18) <= hour <= 22,
        'source': 'GIA-POL-01 sections 2.3, 3.1, 4.2' if current else 'GIA-SUP-12 sections 1–3 (historically applicable v2.0)',
    }


def counter_rule(day: date, hour: int, terminal: str) -> dict:
    source = 'GIA-STD-03 sections 2–3'
    if hour < 5:
        minimum = 4 if terminal == 'T1' else 2
    elif hour < 7:
        minimum = 7 if terminal == 'T1' else 4
    elif terminal == 'T2':
        minimum = 5
    elif 17 <= hour <= 22:
        minimum = 9
        if day.weekday() == 4 and date(2025, 10, 1) <= day <= date(2026, 6, 30):
            minimum = 5
            source = 'GIA-STD-03 section 5.2 (EX-2025-07, expires 30 June 2026)'
    else:
        minimum = 8
    return {'minimum': minimum, 'source': source}


class PolicyRetrievalError(RuntimeError):
    pass


class PolicyStore:
    def __init__(self, folder: Path, *, embed_model: BaseEmbedding, index_path: Path | None = None,
                 chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
                 similarity_top_k: int = DEFAULT_SIMILARITY_TOP_K):
        self.clauses = []
        self.embed_model = embed_model
        self._index = None
        self._index_lock = Lock()
        if similarity_top_k < 1:
            raise ValueError('similarity_top_k must be positive')
        self.similarity_top_k = similarity_top_k
        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        register = folder / 'Document_Register.csv'
        with register.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                text = (folder / row['file']).read_text(encoding='utf-8')
                doc = row['doc_no'].split()[0]
                match_date = re.search(r'\d{1,2} [A-Za-z]+ \d{4}', row['effective'])
                valid_from = datetime.strptime(match_date.group(), '%d %B %Y').date().isoformat() if match_date else None
                for line in text.splitlines():
                    match = re.match(r'^(\d+\.\d+)\s+(.+)', line)
                    if match:
                        self.clauses.append({
                            'document': doc, 'section': match[1],
                            'status': row['status'], 'effective': row['effective'],
                            'effective_from': valid_from,
                            'effective_to': '2025-12-31' if doc == 'GIA-SUP-12' else None,
                            'version': row['version'], 'text': match[2],
                        })

        self.nodes = []
        self._clauses_by_id = {}
        for clause in self.clauses:
            if clause['document'] == 'GIA-DRAFT-11' or clause['status'].upper() == 'DRAFT':
                continue
            # Split within each section so every chunk retains an exact source citation.
            for index, text in enumerate(splitter.split_text(clause['text'])):
                chunk = {**clause, 'text': text}
                node_id = hashlib.sha256(json.dumps([chunk, index], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.nodes.append(TextNode(
                    id_=node_id, text=f"{clause['document']} section {clause['section']}: {text}",
                    metadata=chunk,
                ))
                self._clauses_by_id[node_id] = chunk
        # A document, metadata or embedding-model change selects a new cache.
        fingerprint = hashlib.sha256(json.dumps({
            'schema': 2, 'model': embed_model.model_name, 'adapter': type(embed_model).__name__,
            'chunk_size': chunk_size, 'chunk_overlap': chunk_overlap,
            'clauses': list(self._clauses_by_id.values()),
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.index_path = index_path / fingerprint if index_path else None

    @classmethod
    def from_settings(cls, settings: Settings):
        return cls(settings.documents, embed_model=GeminiPolicyEmbedding(
            settings.api_key, settings.embedding_model, embed_batch_size=settings.embed_batch_size),
            index_path=settings.policy_index, chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap, similarity_top_k=settings.similarity_top_k)

    def _ensure_index(self):
        with self._index_lock:
            if self._index is not None:
                return self._index
            if self.index_path and (self.index_path / '.complete').is_file():
                try:
                    self._index = load_index_from_storage(
                        StorageContext.from_defaults(persist_dir=str(self.index_path)), embed_model=self.embed_model,
                    )
                    return self._index
                except (OSError, ValueError, KeyError):
                    # Interrupted/corrupt caches are rebuilt from the registered sources.
                    pass
            cache_path = self.index_path / 'embeddings.json' if self.index_path else None
            cache = SimpleKVStore()
            if cache_path and cache_path.is_file():
                try:
                    cache = SimpleKVStore.from_persist_path(str(cache_path))
                except (OSError, ValueError):
                    pass
            pending = []
            for node in self.nodes:
                cached = cache.get(node.node_id, collection='policy_embeddings')
                if cached:
                    node.embedding = cached['vector']
                elif node.embedding is None:
                    pending.append(node)
            # Save each successful batch so a provider rate limit never discards index progress.
            for start in range(0, len(pending), self.embed_model.embed_batch_size):
                batch = pending[start:start + self.embed_model.embed_batch_size]
                vectors = self.embed_model.get_text_embedding_batch([
                    node.get_content(metadata_mode=MetadataMode.EMBED) for node in batch
                ])
                for node, vector in zip(batch, vectors, strict=True):
                    node.embedding = vector
                    cache.put(node.node_id, {'vector': vector}, collection='policy_embeddings')
                if cache_path:
                    cache.persist(str(cache_path))
            index = VectorStoreIndex(self.nodes, embed_model=self.embed_model, show_progress=False)
            if self.index_path:
                self.index_path.mkdir(parents=True, exist_ok=True)
                index.storage_context.persist(persist_dir=str(self.index_path))
                (self.index_path / '.complete').touch()
            self._index = index
            return index

    def search(self, query: str, date_from: date, date_to: date) -> list[dict]:
        if not query.strip():
            return []
        candidates = []
        for node_id, clause in self._clauses_by_id.items():
            doc = clause['document']
            if doc == 'GIA-DRAFT-11':
                continue
            if clause['effective_from'] and date.fromisoformat(clause['effective_from']) > date_to:
                continue
            if doc == 'GIA-SUP-12' and date_from > date(2025, 12, 31):
                continue
            if doc == 'GIA-SOP-06' and date_to < date(2026, 3, 1):
                continue
            candidates.append(node_id)
        if not candidates:
            return []
        try:
            # Restrict the vector search BEFORE top-k so invalid policies cannot crowd out valid ones.
            retrieved = VectorIndexRetriever(
                index=self._ensure_index(), similarity_top_k=min(self.similarity_top_k, len(candidates)), node_ids=candidates,
            ).retrieve(query)
        except Exception as exc:
            raise PolicyRetrievalError('Policy vector retrieval is unavailable') from exc
        eligible = set(candidates)
        selected = [self._clauses_by_id[item.node.node_id] for item in retrieved if item.node.node_id in eligible]
        # Preserve date/exception/measurement context even for Arabic queries.
        required = {('GIA-POL-01', '2.3'), ('GIA-POL-01', '4.2'), ('GIA-STD-03', '5.2'), ('GIA-STD-03', '5.3')}
        if date_from < date(2026, 1, 1):
            required |= {('GIA-SUP-12', '1.1'), ('GIA-SUP-12', '2.3')}
        eligible_sections = {(self._clauses_by_id[node_id]['document'], self._clauses_by_id[node_id]['section']) for node_id in candidates}
        # Mandatory rules retain their complete text even when a section spans several chunks.
        selected = [clause for clause in selected if (clause['document'], clause['section']) not in required]
        for clause in self.clauses:
            key = (clause['document'], clause['section'])
            if key in required and key in eligible_sections and clause not in selected:
                selected.append(clause)
        return selected
