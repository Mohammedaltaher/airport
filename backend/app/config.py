from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT / '.env')


def path_setting(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value.resolve() if value.is_absolute() else (PROJECT / value).resolve()


@dataclass
class Settings:
    database: Path = field(default_factory=lambda: path_setting('DMA_DATABASE_PATH', 'backend/data/database/airport_ops.db'))
    documents: Path = field(default_factory=lambda: path_setting('DMA_DOCUMENTS_PATH', 'backend/data/documents'))
    api_key: str = field(default_factory=lambda: os.getenv('GEMINI_API_KEY', ''))
    model: str = field(default_factory=lambda: os.getenv('GEMINI_MODEL', ''))
    origins: list[str] = field(default_factory=lambda: os.getenv('DMA_ALLOWED_ORIGINS', 'http://localhost:4321,http://127.0.0.1:4321').split(','))
    logs: Path = field(default_factory=lambda: PROJECT / 'logs')

    @property
    def assistant_configured(self) -> bool:
        return bool(self.api_key and self.model)
