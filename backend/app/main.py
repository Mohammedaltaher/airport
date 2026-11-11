from contextlib import closing
from io import StringIO
from typing import Annotated
from uuid import UUID
import asyncio
import csv
import sqlite3

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analytics import Analytics
from .assistant import Assistant
from .chat_history import ChatHistory
from .config import PROJECT, Settings
from .models import AssistantRequest, Filters
from .policies import PolicyStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title='DMA Airport Queue and Staffing Assistant', version='1.0.0')
    app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])
    analytics = Analytics(settings)
    history = ChatHistory(settings.chat_database)
    app.state.settings = settings
    app.state.analytics = analytics
    app.state.assistant = None

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=503, content={'detail': 'A database is unavailable or incompatible. Check DMA_DATABASE_PATH and DMA_CHAT_DATABASE_PATH, including directory permissions.'})

    @app.exception_handler(FileNotFoundError)
    async def source_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=503, content={'detail': 'A configured source file is missing. Check the database and document paths.'})

    @app.get('/api/v1/health')
    def health():
        database_ok = False
        try:
            with closing(sqlite3.connect(settings.database.as_uri() + '?mode=ro', uri=True)) as con:
                database_ok = con.execute('SELECT COUNT(*) FROM meta').fetchone()[0] > 0
        except (sqlite3.Error, OSError, ValueError):
            pass
        return {'status': 'ok' if database_ok else 'degraded', 'database_available': database_ok,
                'assistant_configured': settings.assistant_configured,
                'documents_available': (settings.documents / 'Document_Register.csv').is_file(),
                'assistant_message': 'Gemini configured; provider access is checked when answering.' if settings.assistant_configured else 'Set GEMINI_API_KEY and GEMINI_MODEL on the server to enable the assistant.',
                'fictional': True}

    @app.get('/api/v1/metadata')
    def metadata():
        return analytics.metadata()

    @app.get('/api/v1/dashboard')
    def dashboard(filters: Annotated[Filters, Query()]):
        return analytics.dashboard(filters)

    @app.get('/api/v1/findings.csv')
    def findings_csv(filters: Annotated[Filters, Query()]):
        data = analytics.dashboard(filters)
        stream = StringIO(newline='')
        fields = ['kind', 'month', 'terminal', 'weekday', 'hour', 'status', 'breach', 'sample_count', 'average_wait', 'average_limit', 'pct_over_30', 'tail_limit', 'policy', 'partial_month', 'detail', 'missing_hours', 'shortfall_hours', 'exclusions', 'date_from', 'date_to']
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for finding in data['findings']:
            writer.writerow({**finding, 'exclusions': ' | '.join(data['exclusions']), 'date_from': str(filters.date_from), 'date_to': str(filters.date_to)})
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="dma-findings.csv"'})

    @app.get('/api/v1/conversations')
    def conversations(client_id: UUID):
        return {'conversations': history.list(str(client_id))}

    @app.get('/api/v1/conversations/{conversation_id}')
    def conversation(conversation_id: UUID, client_id: UUID):
        saved = history.get(str(client_id), str(conversation_id))
        if saved is None:
            raise HTTPException(404, 'Conversation not found.')
        return saved

    @app.post('/api/v1/assistant')
    async def assistant(request: AssistantRequest):
        if not settings.assistant_configured:
            raise HTTPException(503, 'Gemini is not configured. Set GEMINI_API_KEY and GEMINI_MODEL on the server. Dashboard analysis remains available.')
        previous = None
        if request.conversation_id:
            previous = await asyncio.to_thread(history.get, str(request.client_id), str(request.conversation_id))
            if previous is None:
                raise HTTPException(404, 'Conversation not found.')
        if app.state.assistant is None:
            app.state.assistant = Assistant(settings, analytics, PolicyStore(settings.documents))
        try:
            answer = await app.state.assistant.answer(request, history=previous['messages'] if previous else [])
        except TimeoutError:
            raise HTTPException(504, 'The assistant exceeded its 60-second deadline. Try a narrower question or period.')
        except (sqlite3.Error, FileNotFoundError):
            raise
        except Exception:
            # Never expose provider exceptions: they may contain request credentials.
            raise HTTPException(502, 'Gemini could not complete this request. Check model access, credentials and quota, then retry. Dashboard analysis remains available.')
        conversation_id = await asyncio.to_thread(history.save_exchange, str(request.client_id),
                                                  str(request.conversation_id) if request.conversation_id else None,
                                                  request.question, request.filters.model_dump(mode='json'), answer)
        return {**answer, 'conversation_id': conversation_id}

    build = PROJECT / 'frontend' / 'dist'
    if build.is_dir():
        app.mount('/', StaticFiles(directory=build, html=True), name='frontend')
    else:
        @app.get('/')
        def index():
            return {'app': 'DMA', 'message': 'Run the Astro frontend on http://localhost:4321 or build it and restart this backend.', 'api_docs': '/docs'}
    return app


app = create_app()
