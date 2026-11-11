from datetime import datetime, timezone
from pathlib import Path
import json
import threading

_lock = threading.Lock()


def record(folder: Path, tool: str, request: dict, result):
    folder.mkdir(parents=True, exist_ok=True)
    entry = {'at': datetime.now(timezone.utc).isoformat(), 'tool': tool, 'request': request, 'result': result}
    with _lock, (folder / 'tools.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, default=str, allow_nan=False) + '\n')
