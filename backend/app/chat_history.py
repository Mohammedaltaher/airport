"""Chat persistence, kept separate from the read-only airport source database."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from uuid import uuid4


class ChatHistory:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys = ON')
        con.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, client_id TEXT NOT NULL, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS conversations_client ON conversations(client_id, updated_at);
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL, filters TEXT NOT NULL, citations TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conversation ON chat_messages(conversation_id, id);
        ''')
        return con

    def list(self, client_id: str):
        with closing(self.connect()) as con:
            return [dict(row) for row in con.execute(
                'SELECT id, title, created_at, updated_at FROM conversations WHERE client_id = ? ORDER BY updated_at DESC, id',
                (client_id,),
            )]

    def get(self, client_id: str, conversation_id: str):
        with closing(self.connect()) as con:
            row = con.execute('SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND client_id = ?',
                              (conversation_id, client_id)).fetchone()
            if row is None:
                return None
            messages = []
            for message in con.execute('SELECT role, content, filters, citations, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY id', (conversation_id,)):
                item = dict(message)
                item['filters'] = json.loads(item['filters'])
                item['citations'] = json.loads(item['citations'])
                messages.append(item)
            return {**dict(row), 'messages': messages}

    def save_exchange(self, client_id: str, conversation_id: str | None, question: str, filters: dict, answer: dict):
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as con, con:
            if conversation_id:
                if not con.execute('SELECT 1 FROM conversations WHERE id = ? AND client_id = ?', (conversation_id, client_id)).fetchone():
                    raise ValueError('Conversation not found')
            else:
                conversation_id = str(uuid4())
                title = ' '.join(question.split())[:90]
                con.execute('INSERT INTO conversations VALUES (?, ?, ?, ?, ?)', (conversation_id, client_id, title, now, now))
            con.executemany('INSERT INTO chat_messages (conversation_id, role, content, filters, citations, created_at) VALUES (?, ?, ?, ?, ?, ?)', [
                (conversation_id, 'user', question, json.dumps(filters), '[]', now),
                (conversation_id, 'assistant', answer['answer'], json.dumps(filters), json.dumps(answer.get('citations', []), ensure_ascii=False), now),
            ])
            con.execute('UPDATE conversations SET updated_at = ? WHERE id = ?', (now, conversation_id))
        return conversation_id
