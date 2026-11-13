import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import protected
from app.config import get_settings
from app.db import get_engine
from app.errors import AppError
from app.logging_config import configure_logging
from app.ui import ui

settings = get_settings()
configure_logging(settings.log_level)
app = FastAPI(title="StoryAudit", version="1.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    if request.method == "POST" and request.url.path.startswith("/ui/"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse(status_code=403, content={"error": {"code": "invalid_origin", "message": "Cross-origin form submission rejected", "request_id": request.state.request_id}})
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_request_body_bytes:
        return JSONResponse(status_code=413, content={"error": {"code": "payload_too_large", "message": "Request body is too large", "request_id": request.state.request_id}})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "request_id": request.state.request_id}}, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "Invalid request payload", "request_id": request.state.request_id}})


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/scans")


app.include_router(protected)
app.include_router(ui)
