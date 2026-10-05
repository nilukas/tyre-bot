"""Tyre Advisor: FastAPI BFF serving the chat page and the JSON API defined in openapi.yaml."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import auth
from .analysis import DISCLAIMER, AnalysisUnavailable, build_advisor
from .auth import AuthState, User, current_user, get_auth_state, get_session, require_admin
from .config import get_settings
from .identity import build_azure_credential
from .images import InvalidImage, sanitize_image
from .schemas import (
    AnalysisResult,
    ChatReply,
    ChatRequest,
    Me,
    ReferenceCondition,
    ReferenceList,
    ReferenceTyre,
    RemainingLifeEstimate,
    TyreVisionAssessment,
    VehicleType,
)
from .storage import build_data_store, new_id
from .wear import estimate_remaining_life, summarise

log = logging.getLogger("tyre_advisor")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_CHAT_TURNS = 20

settings = get_settings()
credential = build_azure_credential(settings)

app = FastAPI(
    title="Tyre Advisor",
    version="1.0.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.state.auth = auth.build_auth_state(settings, credential)
app.state.store = build_data_store(settings, credential)
app.state.advisor = build_advisor(settings, credential)
log.warning("Tyre Advisor starting: APP_ENV=%s DATA_BACKEND=%s AI_BACKEND=%s",
            settings.app_env, settings.data_backend, settings.ai_backend)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[urlsplit(settings.public_base_url).hostname or "localhost"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
        "object-src 'none'"
    )
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(self)"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if settings.is_production:
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    if request.url.path.startswith(("/api/", "/auth/")):
        h["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AnalysisUnavailable)
async def _analysis_unavailable(_: Request, exc: AnalysisUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


@app.exception_handler(InvalidImage)
async def _invalid_image(_: Request, exc: InvalidImage):
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)


def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read(settings.max_upload_bytes + 1)  # bounded read
    return sanitize_image(data, max_bytes=settings.max_upload_bytes)


# --------------------------------------------------------------------------
# Auth (BFF)
# --------------------------------------------------------------------------


@app.get("/auth/login", include_in_schema=False)
def login(auth_state: AuthState = Depends(get_auth_state)):
    if settings.dev_auth_bypass:  # APP_ENV=development only (enforced in config)
        sid = auth.create_user_session(auth_state, user_id="dev-user", name="Developer",
                                       roles=[settings.admin_app_role])
        resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        auth.set_session_cookie(resp, auth_state, sid, settings.session_ttl_seconds)
        return resp
    sid, url = auth.start_login(auth_state)
    resp = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    auth.set_session_cookie(resp, auth_state, sid, 600)
    return resp


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(request: Request, auth_state: AuthState = Depends(get_auth_state)):
    sid = auth.finish_login(auth_state, request.cookies.get(auth_state.cookie_name),
                            dict(request.query_params))
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    auth.set_session_cookie(resp, auth_state, sid, settings.session_ttl_seconds)
    return resp


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, _: dict = Depends(get_session),
           auth_state: AuthState = Depends(get_auth_state)):
    auth_state.sessions.delete(request.cookies.get(auth_state.cookie_name))
    auth.clear_session_cookie(response, auth_state)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/api/me", response_model=Me)
def me(user: User = Depends(current_user)):
    return Me(name=user.name, is_admin=user.has_role(settings.admin_app_role),
              csrf_token=user.csrf_token)


@app.post("/api/analyses", response_model=AnalysisResult)
def create_analysis(
    image: Annotated[UploadFile, File()],
    vehicle_type: Annotated[VehicleType, Form()] = VehicleType.car,
    km_since_fitted: Annotated[int | None, Form(ge=0, le=300_000)] = None,
    tread_when_new_mm: Annotated[float | None, Form(ge=4, le=20)] = None,
    question: Annotated[str | None, Form(max_length=2000)] = None,
    user: User = Depends(current_user),
    auth_state: AuthState = Depends(get_auth_state),
):
    auth_state.analysis_limiter.check(user.user_id)
    jpeg = _read_upload(image)
    store = app.state.store

    analysis_id = new_id()
    store.save_upload(user.user_id, analysis_id, jpeg)

    assessment = app.state.advisor.assess(store, jpeg, vehicle_type)
    estimate = estimate_remaining_life(
        assessment,
        legal_min_mm=settings.legal_min_tread_mm,
        recommended_min_mm=settings.recommended_min_tread_mm,
        default_new_mm=settings.default_new_tread_mm,
        default_km_per_mm=settings.default_km_per_mm,
        vehicle_type=vehicle_type,
        km_since_fitted=km_since_fitted,
        tread_when_new_mm=tread_when_new_mm,
    )

    store.save_analysis(user.user_id, analysis_id, vehicle_type.value,
                        assessment.model_dump(mode="json"), estimate.model_dump(mode="json"))

    reply = None
    if question and question.strip():
        reply = app.state.advisor.chat(assessment, estimate, [], question.strip())
        store.append_messages(user.user_id, analysis_id,
                              [{"role": "user", "content": question.strip()},
                               {"role": "assistant", "content": reply}])
    log.info("analysis %s verdict=%s confidence=%s", analysis_id, estimate.verdict.value,
             assessment.confidence.value)

    return AnalysisResult(analysis_id=analysis_id, assessment=assessment, estimate=estimate,
                          summary=summarise(estimate), reply=reply, disclaimer=DISCLAIMER)


@app.post("/api/analyses/{analysis_id}/messages", response_model=ChatReply)
def chat(analysis_id: str, body: ChatRequest, user: User = Depends(current_user)):
    store = app.state.store
    record = store.get_analysis(user.user_id, analysis_id)  # owner-scoped lookup
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found.")
    if len(record["history"]) >= MAX_CHAT_TURNS * 2:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "This conversation is long. Please upload the photo again to start fresh.")
    question = body.message.strip()
    reply = app.state.advisor.chat(
        TyreVisionAssessment.model_validate(record["assessment"]),
        RemainingLifeEstimate.model_validate(record["estimate"]),
        record["history"], question,
    )
    store.append_messages(user.user_id, analysis_id,
                          [{"role": "user", "content": question},
                           {"role": "assistant", "content": reply}])
    return ChatReply(reply=reply)


# ---- Reference library (staff only) --------------------------------------


@app.get("/api/references", response_model=ReferenceList)
def list_references(_: User = Depends(require_admin)):
    items = sorted(app.state.store.list_references(), key=lambda r: r.created_at, reverse=True)
    return ReferenceList(items=items)


@app.post("/api/references", response_model=ReferenceTyre, status_code=status.HTTP_201_CREATED)
def create_reference(
    image: Annotated[UploadFile, File()],
    condition: Annotated[ReferenceCondition, Form()],
    tread_depth_mm: Annotated[float, Form(ge=0, le=20)],
    km_driven: Annotated[int | None, Form(ge=0, le=500_000)] = None,
    tyre_size: Annotated[str | None, Form(max_length=40, pattern=r"^[0-9A-Za-z /.\-]*$")] = None,
    brand_model: Annotated[str | None, Form(max_length=80)] = None,
    notes: Annotated[str | None, Form(max_length=500)] = None,
    user: User = Depends(require_admin),
):
    jpeg = _read_upload(image)
    ref = ReferenceTyre(
        id=new_id(), condition=condition, tread_depth_mm=tread_depth_mm, km_driven=km_driven,
        tyre_size=tyre_size or None, brand_model=brand_model or None, notes=notes or None,
        created_by=user.name, created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    app.state.store.save_reference(ref, jpeg)
    log.info("reference %s added by %s", ref.id, user.user_id)
    return ref


@app.get("/api/references/{ref_id}/image", responses={200: {"content": {"image/jpeg": {}}}})
def reference_image(ref_id: str, _: User = Depends(require_admin)):
    try:
        data = app.state.store.get_reference_image(ref_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from None
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.delete("/api/references/{ref_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reference(ref_id: str, user: User = Depends(require_admin)):
    try:
        app.state.store.delete_reference(ref_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from None
    log.info("reference %s deleted by %s", ref_id, user.user_id)


# --------------------------------------------------------------------------
# Static pages
# --------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
