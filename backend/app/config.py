from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT / '.env')

DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 64
DEFAULT_SIMILARITY_TOP_K = 14
DEFAULT_EMBED_BATCH_SIZE = 16


def path_setting(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value.resolve() if value.is_absolute() else (PROJECT / value).resolve()


@dataclass
class Settings:
    database: Path = field(default_factory=lambda: path_setting('DMA_DATABASE_PATH', 'backend/data/database/airport_ops.db'))
    chat_database: Path = field(default_factory=lambda: path_setting('DMA_CHAT_DATABASE_PATH', 'backend/data/chat/history.db'))
    documents: Path = field(default_factory=lambda: path_setting('DMA_DOCUMENTS_PATH', 'backend/data/documents'))
    policy_index: Path = field(default_factory=lambda: path_setting('DMA_POLICY_INDEX_PATH', 'backend/data/policy_index'))
    embedding_model: str = field(default_factory=lambda: os.getenv('GEMINI_EMBEDDING_MODEL', 'gemini-embedding-001'))
    chunk_size: int = field(default_factory=lambda: int(os.getenv('DMA_CHUNK_SIZE', str(DEFAULT_CHUNK_SIZE))))
    chunk_overlap: int = field(default_factory=lambda: int(os.getenv('DMA_CHUNK_OVERLAP', str(DEFAULT_CHUNK_OVERLAP))))
    similarity_top_k: int = field(default_factory=lambda: int(os.getenv('DMA_SIMILARITY_TOP_K', str(DEFAULT_SIMILARITY_TOP_K))))
    embed_batch_size: int = field(default_factory=lambda: int(os.getenv('DMA_EMBED_BATCH_SIZE', str(DEFAULT_EMBED_BATCH_SIZE))))
    api_key: str = field(default_factory=lambda: os.getenv('GEMINI_API_KEY', ''))
    model: str = field(default_factory=lambda: os.getenv('GEMINI_MODEL', ''))
    origins: list[str] = field(default_factory=lambda: os.getenv('DMA_ALLOWED_ORIGINS', 'http://localhost:4321,http://127.0.0.1:4321').split(','))
    logs: Path = field(default_factory=lambda: PROJECT / 'logs')

    def __post_init__(self):
        if self.chunk_size < 1 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError('DMA_CHUNK_SIZE must be positive and DMA_CHUNK_OVERLAP must be between 0 and chunk size minus 1.')
        if self.similarity_top_k < 1:
            raise ValueError('DMA_SIMILARITY_TOP_K must be positive.')
        if not 1 <= self.embed_batch_size <= 2048:
            raise ValueError('DMA_EMBED_BATCH_SIZE must be between 1 and 2048.')

    @property
    def assistant_configured(self) -> bool:
        return bool(self.api_key and self.model)
