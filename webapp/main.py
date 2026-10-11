import hashlib
import hmac
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from webapp.service import TOOLS, TOOLS_BY_ID, JobError, run_job

PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET = os.environ.get("SESSION_SECRET") or PASSWORD
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "150")) * 1024 * 1024
COOKIE = "xcoder_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30

_job_slots = threading.BoundedSemaphore(int(os.environ.get("MAX_JOBS", "2")))
_failures: dict[str, list[float]] = {}

app = FastAPI(title="XCoder", docs_url=None, redoc_url=None, openapi_url=None)


def _token() -> str:
    return hmac.new(
        SECRET.encode(), b"xcoder-session-v1:" + PASSWORD.encode(), hashlib.sha256
    ).hexdigest()


def _is_authed(request: Request) -> bool:
    if not PASSWORD:
        return False
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


def require_auth(request: Request) -> None:
    if not _is_authed(request):
        raise HTTPException(401, "Not signed in.")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


class LoginBody(BaseModel):
    password: str


@app.get("/api/me")
def me(request: Request):
    return {"configured": bool(PASSWORD), "authed": _is_authed(request)}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    if not PASSWORD:
        raise HTTPException(503, "Server has no APP_PASSWORD set.")

    ip, now = _client_ip(request), time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")

    if not hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        _failures[ip] = recent + [now]
        time.sleep(1)
        raise HTTPException(401, "Wrong password.")

    _failures.pop(ip, None)
    response.set_cookie(
        COOKIE,
        _token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/tools", dependencies=[Depends(require_auth)])
def tools():
    return [{"id": t.id, "label": t.label, "help": t.help} for t in TOOLS]


@app.post("/api/run", dependencies=[Depends(require_auth)])
def run(tool: str = Form(...), files: list[UploadFile] = File(...)):
    if tool not in TOOLS_BY_ID:
        raise HTTPException(400, "Unknown tool.")
    if not _job_slots.acquire(blocking=False):
        raise HTTPException(429, "Server is busy with other jobs. Try again shortly.")

    work = Path(tempfile.mkdtemp(prefix="xcoder-"))
    try:
        raw = work / "raw"
        raw.mkdir()
        total = 0
        for upload in files:
            name = Path(upload.filename or "file").name
            with open(raw / name, "wb") as out:
                while chunk := upload.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            413, f"Upload is over {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
                        )
                    out.write(chunk)

        zip_path, _log = run_job(tool, raw, work)
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"xcoder-{tool}.zip",
            background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),
        )
    except JobError as error:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(422, str(error)[:4000])
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        _job_slots.release()


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static"
)
