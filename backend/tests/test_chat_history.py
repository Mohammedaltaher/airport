from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import sqlite3

from fastapi.testclient import TestClient

from dma.backend.app.config import Settings
from dma.backend.app.main import create_app
from dma.backend.app.policies import PolicyRetrievalError


def test_conversations_persist_and_are_scoped_to_browser(tmp_path):
    settings = Settings(chat_database=tmp_path / 'chat' / 'history.db', api_key='test', model='mock')
    original = {'answer': '**Finding:** مراجعة staffing', 'citations': [{'document': 'POL', 'section': '2', 'text': 'Evidence'}]}
    app = create_app(settings)
    provider = SimpleNamespace(answer=AsyncMock(return_value=original))
    app.state.assistant = provider
    client = TestClient(app)
    owner, other = str(uuid4()), str(uuid4())
    assert client.get('/api/v1/conversations', params={'client_id': owner}).json() == {'conversations': []}
    response = client.post('/api/v1/assistant', json={'client_id': owner, 'question': 'Review staffing'})
    assert response.status_code == 200
    conversation_id = response.json()['conversation_id']
    assert provider.answer.await_args.kwargs['history'] == []
    # A second app represents a server restart, reading the same independent SQLite file.
    restarted = create_app(settings)
    restarted.state.assistant = provider
    client = TestClient(restarted)
    listing = client.get('/api/v1/conversations', params={'client_id': owner}).json()['conversations']
    assert listing[0]['id'] == conversation_id and listing[0]['title'] == 'Review staffing'
    saved = client.get(f'/api/v1/conversations/{conversation_id}', params={'client_id': owner}).json()
    assert [item['role'] for item in saved['messages']] == ['user', 'assistant']
    assert saved['messages'][1]['citations'] == original['citations']
    assert saved['messages'][1]['content'] == original['answer']
    assert saved['messages'][0]['filters']['terminal'] == 'T1'
    followup = client.post('/api/v1/assistant', json={'client_id': owner, 'conversation_id': conversation_id, 'question': 'How do we fix this?'})
    assert followup.status_code == 200 and followup.json()['conversation_id'] == conversation_id
    assert len(provider.answer.await_args.kwargs['history']) == 2
    assert client.get('/api/v1/conversations', params={'client_id': other}).json()['conversations'] == []
    assert client.get(f'/api/v1/conversations/{conversation_id}', params={'client_id': other}).status_code == 404
    assert client.post('/api/v1/assistant', json={'client_id': other, 'conversation_id': conversation_id, 'question': 'Read another chat'}).status_code == 404
    assert client.get('/api/v1/conversations', params={'client_id': 'invalid'}).status_code == 422
    with sqlite3.connect(settings.chat_database) as con:
        assert con.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0] == 4


def test_failed_answer_does_not_create_completed_history(tmp_path):
    settings = Settings(chat_database=tmp_path / 'history.db', api_key='test', model='mock')
    app = create_app(settings)
    app.state.assistant = SimpleNamespace(answer=AsyncMock(side_effect=TimeoutError()))
    client = TestClient(app)
    owner = str(uuid4())
    assert client.post('/api/v1/assistant', json={'client_id': owner, 'question': 'Review queues'}).status_code == 504
    assert client.get('/api/v1/conversations', params={'client_id': owner}).json()['conversations'] == []


def test_embedding_rate_limit_returns_actionable_error_without_provider_secrets(tmp_path):
    settings = Settings(chat_database=tmp_path / 'history.db', api_key='test-secret', model='mock')
    app = create_app(settings)
    cause = RuntimeError('test-secret provider detail')
    cause.code = 429
    failure = PolicyRetrievalError('Retrieval failed')
    failure.__cause__ = cause
    app.state.assistant = SimpleNamespace(answer=AsyncMock(side_effect=failure))
    response = TestClient(app).post('/api/v1/assistant', json={'question': 'Review queues'})
    assert response.status_code == 429
    assert response.headers['retry-after'] == '60'
    assert 'one minute' in response.json()['detail']
    assert 'test-secret' not in response.text
