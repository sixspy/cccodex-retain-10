from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import get_db, init_db
from app.services import (
    advance_progress,
    annotate_judgments,
    annotate_partitions,
    build_card,
    execute_judgment,
    list_advance_logs,
    list_cards,
    list_judgments,
    list_partitions,
    list_policies,
    list_reports,
    list_runs,
    list_scan_jobs,
    list_watermarks,
    run_expire_scan,
    run_reconcile,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-retain-session")

app = FastAPI(title="cc-retain-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("retain_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        "retain_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("retain_session")
    return resp


@app.get("/api/meta")
def api_meta(request: Request):
    require_user(request)
    return {
        "lab_name": Config.LAB_NAME,
        "lab_code": Config.LAB_CODE,
        "skeleton": "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
    }


@app.get("/api/policies")
def api_policies(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_policies(conn, active_only=True)}


@app.get("/api/partitions")
def api_partitions(
    request: Request,
    domain_code: str | None = None,
    as_of_date: str | None = None,
):
    require_user(request)
    with get_db() as conn:
        items = list_partitions(conn, domain_code=domain_code)
        items = annotate_partitions(conn, items, as_of_date)
    return {"items": items}


@app.get("/api/scan-jobs")
def api_scan_jobs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_scan_jobs(conn)}


@app.post("/api/scan-jobs")
async def api_create_scan(request: Request):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    with get_db() as conn:
        job = run_expire_scan(conn, as_of)
    return {"ok": True, "job": job}


@app.get("/api/judgments")
def api_judgments(
    request: Request,
    status: str | None = None,
    as_of_date: str | None = None,
):
    require_user(request)
    with get_db() as conn:
        items = list_judgments(conn, status=status)
        items = annotate_judgments(conn, items, as_of_date)
    return {"items": items}


@app.post("/api/judgments/{judgment_id}/execute")
def api_execute(request: Request, judgment_id: int):
    require_user(request)
    try:
        with get_db() as conn:
            run = execute_judgment(conn, judgment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "run": run}


@app.get("/api/runs")
def api_runs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_runs(conn)}


@app.get("/api/reconcile-reports")
def api_reports(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_reports(conn)}


@app.post("/api/reconcile-reports")
async def api_reconcile(request: Request):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    with get_db() as conn:
        report = run_reconcile(conn, as_of)
    return {"ok": True, "report": report}


@app.get("/api/progress-cards")
def api_progress_cards(request: Request, as_of_date: str | None = None):
    require_user(request)
    try:
        with get_db() as conn:
            items = list_cards(conn, as_of_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items}


@app.get("/api/progress-cards/{domain_code}")
def api_progress_card(
    request: Request, domain_code: str, as_of_date: str | None = None
):
    require_user(request)
    try:
        with get_db() as conn:
            card = build_card(conn, domain_code, as_of_date)
    except ValueError as exc:
        if "基准日" in str(exc) and "格式" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "card": card}


@app.post("/api/progress-cards/{domain_code}/advance")
async def api_advance_progress(request: Request, domain_code: str):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    try:
        with get_db() as conn:
            result = advance_progress(conn, domain_code, as_of)
    except ValueError as exc:
        detail = str(exc)
        status = 400 if "格式" in detail else 409
        raise HTTPException(status_code=status, detail=detail) from exc
    return result


@app.get("/api/watermarks")
def api_watermarks(request: Request, domain_code: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_watermarks(conn, domain_code=domain_code)}


@app.get("/api/progress-logs")
def api_progress_logs(request: Request, domain_code: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_advance_logs(conn, domain_code=domain_code)}
