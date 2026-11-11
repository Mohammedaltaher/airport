from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import csv
import io
import sqlite3

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from google.genai import types

from dma.backend.app.analytics import Analytics, queue_statistics
from dma.backend.app.assistant import Assistant
from dma.backend.app.config import PROJECT, Settings
from dma.backend.app.main import create_app
from dma.backend.app.models import AssistantRequest, Filters
from dma.backend.app.policies import PolicyStore, counter_rule, service_rule


@pytest.fixture
def settings(tmp_path):
    database = tmp_path / 'airport.db'
    with sqlite3.connect(database) as con:
        con.executescript('''
            CREATE TABLE meta (key TEXT, value TEXT);
            INSERT INTO meta VALUES ('fictional', 'true');
            CREATE TABLE passenger_journeys (
                journey_date TEXT, weekday TEXT, immigration_hour INTEGER,
                terminal_id TEXT, direction TEXT, immigration_wait_min REAL,
                used_smart_gate INTEGER, missed_flight INTEGER);
            CREATE TABLE staffing_roster (roster_date TEXT, hour INTEGER, checkpoint_id TEXT, counters_open INTEGER);
            CREATE TABLE incidents (incident_id TEXT, start_date TEXT, start_hour INTEGER, duration_min INTEGER,
                                   terminal_id TEXT, incident_type TEXT, severity TEXT);
        ''')
        # Friday exception is expired. Exact boundary waits must not breach.
        con.executemany('INSERT INTO passenger_journeys VALUES (?,?,?,?,?,?,?,?)', [
            ('2026-09-04', 'Friday', 17, 'T1', 'Departure', 15, 0, 0),
            ('2026-09-04', 'Friday', 17, 'T1', 'Departure', 15, 1, 0),
            ('2026-09-04', 'Friday', 18, 'T1', 'Departure', 31, 0, 0),
            ('2026-09-04', 'Friday', 19, 'T1', 'Departure', 31, 0, 0),
            ('2026-09-04', 'Friday', 20, 'T1', 'Departure', 31, 0, 0),
            ('2026-09-05', 'Saturday', 17, 'T1', 'Departure', 45, 0, 0),
            ('2026-09-05', 'Saturday', 18, 'T1', 'Departure', 46, 0, 1),
        ])
        con.executemany('INSERT INTO staffing_roster VALUES (?,?,?,?)', [
            ('2026-09-04', 17, 'T1-IMM-DEP', 5),
            ('2026-09-04', 18, 'T1-IMM-DEP', 9),
            ('2026-09-04', 19, 'T1-IMM-DEP', 9),
            ('2026-09-04', 20, 'T1-IMM-DEP', 9),
            ('2026-09-05', 17, 'T1-IMM-DEP', 9),
        ])
        # Begins before the selected day and spans midnight.
        con.execute("INSERT INTO incidents VALUES ('I-1','2026-09-03',23,1140,'ALL','IT outage','High')")
    return Settings(database=database, chat_database=tmp_path / 'chat' / 'history.db', documents=PROJECT / 'backend/data/documents', api_key='', model='', logs=tmp_path / 'logs')


@pytest.mark.parametrize('day,average,tail,peak', [
    (date(2025, 12, 31), 20, 10, False),
    (date(2026, 1, 1), 15, 5, True),
])
def test_historical_service_rule(day, average, tail, peak):
    rule = service_rule(day, 17)
    assert (rule['average_limit'], rule['tail_limit'], rule['peak']) == (average, tail, peak)


@pytest.mark.parametrize('day,hour,terminal,minimum', [
    (date(2026, 6, 26), 17, 'T1', 5),
    (date(2026, 7, 3), 17, 'T1', 9),
    (date(2026, 7, 3), 17, 'T2', 5),
    (date(2026, 7, 3), 4, 'T1', 4),
    (date(2026, 7, 3), 5, 'T1', 7),
    (date(2026, 7, 3), 7, 'T1', 8),
    (date(2026, 7, 3), 23, 'T1', 8),
])
def test_counter_exception_and_time_boundaries(day, hour, terminal, minimum):
    assert counter_rule(day, hour, terminal)['minimum'] == minimum


def test_scaling_and_strict_wait_threshold():
    stats = queue_statistics(pd.DataFrame({'immigration_wait_min': [30, 31, None]}))
    assert stats['estimated_affected_passengers'] == 25
    assert stats['pct_over_30'] == 50
    assert stats['average_wait'] == 30.5
    assert stats['sampled_passengers'] == 3
    assert stats['measured_passengers'] == 2


def test_independent_rosters_missing_and_smart_gates(settings):
    result = Analytics(settings).dashboard(Filters(date_from=date(2026, 9, 4), date_to=date(2026, 9, 5), hour_from=17, hour_to=18))
    m = result['metrics']
    assert m['sampled_passengers'] == 5  # Smart gate journey retained.
    assert m['average_wait'] == 30.4
    assert m['expected_roster_hours'] == 4
    assert m['observed_roster_hours'] == 3
    assert m['missing_roster_hours'] == 1
    assert m['shortfall_hours'] == 1
    assert m['roster_coverage_pct'] == 75
    chart = next(row for row in result['counter_chart'] if row['hour'] == 17)
    assert chart['average_open'] == 7  # Not passenger-weighted (5+9)/2.
    queue = next(row for row in result['findings'] if row['kind'] == 'Queue' and row['weekday'] == 'Friday' and row['hour'] == 17)
    assert not queue['breach']  # Average exactly 15.
    assert queue['partial_month']
    assert result['incidents'][0]['incident_id'] == 'I-1'


def test_exact_tail_percentage_and_full_precision_average(settings):
    with sqlite3.connect(settings.database) as con:
        con.execute('DELETE FROM passenger_journeys')
        values = [31] + [0] * 19
        con.executemany('INSERT INTO passenger_journeys VALUES (?,?,?,?,?,?,?,?)', [
            ('2026-09-04', 'Friday', 17, 'T1', 'Departure', value, 0, 0) for value in values
        ])
    analytics = Analytics(settings)
    result = analytics.dashboard(Filters())
    queue = next(item for item in result['findings'] if item['kind'] == 'Queue')
    assert queue['pct_over_30'] == 5
    assert not queue['breach']
    with sqlite3.connect(settings.database) as con:
        con.execute('UPDATE passenger_journeys SET immigration_wait_min=15.001')
    queue = next(item for item in analytics.dashboard(Filters())['findings'] if item['kind'] == 'Queue')
    assert queue['average_wait'] == 15
    assert queue['breach']  # Display rounding does not change comparisons.


def test_cross_month_policy_and_exception(settings):
    with sqlite3.connect(settings.database) as con:
        con.executemany('INSERT INTO passenger_journeys VALUES (?,?,?,?,?,?,?,?)', [
            ('2025-12-26', 'Friday', 18, 'T1', 'Departure', 18, 0, 0),
            ('2026-01-02', 'Friday', 18, 'T1', 'Departure', 18, 0, 0),
        ])
    result = Analytics(settings).dashboard(Filters(date_from=date(2025, 12, 1), date_to=date(2026, 1, 31), hour_from=18, hour_to=18))
    queues = [item for item in result['findings'] if item['kind'] == 'Queue']
    assert len(queues) == 2
    assert queues[0]['average_limit'] == 20 and not queues[0]['breach']
    assert queues[1]['average_limit'] == 15 and queues[1]['breach']
    assert not any(item['partial_month'] for item in queues)


def test_escalation_context_and_strict_boundaries(settings):
    result = Analytics(settings).dashboard(Filters(date_from=date(2026, 9, 4), date_to=date(2026, 9, 5), hour_from=20, hour_to=20))
    assert any(item['level'].startswith('Level 2') for item in result['escalations'])
    result = Analytics(settings).dashboard(Filters(date_from=date(2026, 9, 5), date_to=date(2026, 9, 5), hour_from=17, hour_to=18))
    level3 = [item for item in result['escalations'] if item['level'] == 'Level 3']
    assert len(level3) == 1 and level3[0]['hour'] == 18


def test_escalation_cross_midnight():
    sample = pd.DataFrame([
        {'terminal_id': 'T1', 'journey_date': day, 'immigration_hour': hour, 'immigration_wait_min': wait}
        for day, hour, wait in [('2026-09-03', 22, 31), ('2026-09-03', 23, 31), ('2026-09-04', 0, 31)]
    ])
    events = Analytics.escalations(sample, Filters(date_from=date(2026, 9, 4), date_to=date(2026, 9, 4), hour_from=0, hour_to=0))
    assert any(item['level'].startswith('Level 2') for item in events)
    sample.loc[1, 'immigration_hour'] = 21
    assert not any(item['level'].startswith('Level 2') for item in Analytics.escalations(sample, Filters(hour_from=0, hour_to=0)))


def test_api_validation_export_and_unconfigured_assistant(settings):
    client = TestClient(create_app(settings))
    assert client.get('/api/v1/health').json()['database_available']
    for params in [{'terminal': 'T3'}, {'hour_from': 24}, {'hour_from': 22, 'hour_to': 17}, {'date_from': '2026-10-01', 'date_to': '2026-09-01'}, {'weekday': 'Funday'}]:
        assert client.get('/api/v1/dashboard', params=params).status_code == 422
    assert client.get('/api/v1/dashboard', params={'date_from': '2027-01-01', 'date_to': '2027-01-31'}).json()['metrics']['average_wait'] is None
    response = client.get('/api/v1/findings.csv')
    assert response.status_code == 200
    records = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert records and 'policy' in records[0] and 'exclusions' in records[0]
    assert client.post('/api/v1/assistant', json={'question': 'Review queues'}).status_code == 503
    assert client.post('/api/v1/assistant', json={'question': 'x'}).status_code == 422
    with sqlite3.connect(settings.database.as_uri() + '?mode=ro', uri=True) as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute('DELETE FROM meta')


def test_missing_database_is_degraded(settings):
    settings.database = settings.database.parent / 'missing.db'
    client = TestClient(create_app(settings))
    assert client.get('/api/v1/health').json()['status'] == 'degraded'
    assert client.get('/api/v1/dashboard').status_code == 503


def test_policy_retrieval_statuses_and_arabic(settings):
    store = PolicyStore(settings.documents)
    current = store.search('ما هي سياسة الانتظار؟', date(2026, 9, 1), date(2026, 9, 30))
    assert all(clause['document'] not in {'GIA-DRAFT-11', 'GIA-SUP-12'} for clause in current)
    assert any(c['document'] == 'GIA-STD-03' and c['section'] == '5.3' for c in current)
    historical = store.search('immigration wait', date(2025, 12, 1), date(2025, 12, 31))
    assert any(c['document'] == 'GIA-SUP-12' for c in historical)
    assert not any(c['document'] == 'GIA-SOP-06' for c in historical)


def test_mocked_gemini_tools_and_audit(settings):
    assistant = Assistant(settings, Analytics(settings), PolicyStore(settings.documents))
    call = types.FunctionCall(name='staffing_analysis', args={})
    tool_response = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content=types.Content(role='model', parts=[types.Part(function_call=call)]))])
    final = SimpleNamespace(function_calls=[], text='Finding: one recorded gap. Policy: GIA-STD-03 §5.3. Limitations: missing hours.')
    assistant.gateway.generate = AsyncMock(side_effect=[tool_response, final])
    answer = asyncio.run(assistant.answer(AssistantRequest(question='Review staffing')))
    assert answer['tools_used'] == ['staffing_analysis']
    assert answer['citations']
    assert 'missing' in answer['answer']
    contents = assistant.gateway.generate.await_args_list[1].args[0]
    tool_results = [content for content in contents if any(part.function_response for part in content.parts)]
    assert tool_results and all(content.role == 'user' for content in tool_results)
    assert 'staffing_analysis' in (settings.logs / 'tools.jsonl').read_text(encoding='utf-8')


def test_assistant_provider_failure_timeout_and_round_limit(settings):
    settings.api_key, settings.model = 'test-secret-never-expose', 'mock'
    app = create_app(settings)
    fake = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError('test-secret-never-expose')))
    app.state.assistant = fake
    client = TestClient(app)
    response = client.post('/api/v1/assistant', json={'question': 'Review queues'})
    assert response.status_code == 502 and 'test-secret' not in response.text
    fake.answer = AsyncMock(side_effect=TimeoutError())
    assert client.post('/api/v1/assistant', json={'question': 'Review queues'}).status_code == 504
    assistant = Assistant(settings, Analytics(settings), PolicyStore(settings.documents))
    call = types.FunctionCall(name='queue_analysis', args={})
    response = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content=types.Content(role='model', parts=[types.Part(function_call=call)]))])
    assistant.gateway.generate = AsyncMock(return_value=response)
    with pytest.raises(RuntimeError, match='Tool-round limit'):
        asyncio.run(assistant.answer(AssistantRequest(question='Review queues')))
    assert assistant.gateway.generate.await_count == 6


def test_assistant_uses_bounded_conversation_context_with_fresh_evidence(settings):
    assistant = Assistant(settings, Analytics(settings), PolicyStore(settings.documents))
    captured = []

    async def generate(contents, use_tools=True):
        captured.extend(contents)
        return SimpleNamespace(function_calls=[], text='Verified follow-up')

    assistant.gateway.generate = generate
    history = [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': f'Past message {index}',
                'filters': Filters().model_dump(mode='json')} for index in range(16)]
    asyncio.run(assistant.answer(AssistantRequest(question='What next?'), history=history))
    assert len(captured) == 13
    assert captured[0].role == 'user' and 'Past message 4' in captured[0].parts[0].text
    assert captured[1].role == 'model'
    assert 'verified_context' in captured[-1].parts[0].text and 'What next?' in captured[-1].parts[0].text


def test_real_database_independent_sql(tmp_path):
    settings = Settings(api_key='', model='', logs=tmp_path / 'logs')
    if not settings.database.exists():
        pytest.skip('Restore the supplied workshop database to run the integration check')
    result = Analytics(settings).dashboard(Filters())
    with sqlite3.connect(settings.database.as_uri() + '?mode=ro', uri=True) as con:
        count, average, tail = con.execute('''SELECT COUNT(*), AVG(immigration_wait_min),
            SUM(CASE WHEN immigration_wait_min > 30 THEN 1 ELSE 0 END)
            FROM passenger_journeys WHERE terminal_id='T1' AND direction='Departure'
            AND journey_date BETWEEN '2026-09-01' AND '2026-09-30' AND immigration_hour BETWEEN 17 AND 22''').fetchone()
        roster_count = con.execute("SELECT COUNT(*) FROM staffing_roster WHERE checkpoint_id='T1-IMM-DEP' AND roster_date BETWEEN '2026-09-01' AND '2026-09-30' AND hour BETWEEN 17 AND 22").fetchone()[0]
    assert result['metrics']['sampled_passengers'] == count
    assert result['metrics']['average_wait'] == round(average, 2)
    assert result['metrics']['estimated_affected_passengers'] == tail * 25
    assert result['metrics']['pct_over_30'] == round(tail / count * 100, 2)
    assert result['metrics']['observed_roster_hours'] == roster_count
