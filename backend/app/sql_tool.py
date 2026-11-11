"""One read-only SELECT for the assistant. The chat database is never opened."""
from contextlib import closing
import sqlite3

import pandas as pd

from .config import Settings

SELECT_ONLY = 'Error: only a single SELECT query is allowed.'


def query_airport_database(settings: Settings, sql: str) -> str:
    text = sql.strip().rstrip(';')
    if not text.lower().startswith(('select', 'with')) or ';' in text:
        return SELECT_ONLY
    try:
        with closing(sqlite3.connect(settings.database.as_uri() + '?mode=ro', uri=True)) as con:
            frame = pd.read_sql_query(text, con)
    except Exception as error:
        return f'SQL error: {error}. Check the table and column names and try again.'
    if frame.empty:
        return 'The query returned no rows.'
    note = f'\n(first 30 of {len(frame)} rows shown)' if len(frame) > 30 else ''
    return frame.head(30).to_string(index=False) + note
