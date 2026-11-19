"""地籍变更 Web 应用：导入、预核验、确认提交、现势/历史/变更记录查询、图幅配准。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, reg_db
from .geometry import GeoValidationError, PlanValidationError

DB_PATH = os.environ.get("CADASTRE_DB", "/data/cadastre.db")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def get_conn() -> db.sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    reg_db.init_reg_db(conn)
    return conn


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = get_conn()
    conn.close()
    yield


app = FastAPI(title="地籍变更系统", version="1.0", lifespan=lifespan)


@app.exception_handler(GeoValidationError)
def _geo_error_handler(_: Request, exc: GeoValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "数据校验失败", "detail": str(exc)})


@app.exception_handler(json.JSONDecodeError)
def _json_decode_handler(_: Request, exc: json.JSONDecodeError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "数据校验失败", "detail": f"请求体不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"},
    )


@app.exception_handler(PlanValidationError)
def _plan_error_handler(_: Request, exc: PlanValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "变更前提不成立", "detail": str(exc)})


@app.exception_handler(db.VersionConflictError)
def _conflict_handler(_: Request, exc: db.VersionConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "版本冲突", "detail": str(exc)})


@app.get("/api/state")
def state() -> dict:
    conn = get_conn()
    try:
        parcels = db.list_current(conn)
        return {
            "current_version": db.current_version(conn),
            "parcel_count": len(parcels),
            "parcels": parcels,
        }
    finally:
        conn.close()


@app.post("/api/import")
async def import_geojson(request: Request) -> dict:
    payload = await request.json()
    conn = get_conn()
    try:
        return db.import_initial(conn, payload)
    finally:
        conn.close()


@app.post("/api/changes/plan")
async def plan_change(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.plan(conn, req)
    finally:
        conn.close()


@app.post("/api/changes/commit")
async def commit_change(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.commit_change(conn, req)
    finally:
        conn.close()


@app.get("/api/history/parcel/{parcel_id}")
def history_parcel(parcel_id: str) -> dict:
    conn = get_conn()
    try:
        return db.parcel_history(conn, parcel_id)
    finally:
        conn.close()


@app.get("/api/history/version/{version}")
def history_version(version: int) -> dict:
    if version < 1:
        raise PlanValidationError("版本号必须 >= 1")
    conn = get_conn()
    try:
        return {
            "version": version,
            "parcels": db.snapshot_at(conn, version),
        }
    finally:
        conn.close()


@app.get("/api/txns")
def txns() -> dict:
    conn = get_conn()
    try:
        return {"txns": db.list_txns(conn)}
    finally:
        conn.close()


@app.get("/api/txns/{txn_id}")
def txn_detail(txn_id: int) -> dict:
    conn = get_conn()
    try:
        return db.get_txn(conn, txn_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- 图幅配准
@app.post("/api/registration/jobs")
async def create_registration_job(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return reg_db.create_job(conn, req)
    finally:
        conn.close()


@app.get("/api/registration/jobs")
def list_registration_jobs() -> dict:
    conn = get_conn()
    try:
        return {"jobs": reg_db.list_jobs(conn)}
    finally:
        conn.close()


@app.get("/api/registration/jobs/{job_id}")
def get_registration_job(job_id: int) -> dict:
    conn = get_conn()
    try:
        return reg_db.get_job(conn, job_id)
    finally:
        conn.close()


@app.post("/api/registration/jobs/{job_id}/recompute")
async def recompute_registration_job(job_id: int, request: Request) -> dict:
    body = await request.json()
    conn = get_conn()
    try:
        # 空对象 {} 表示仅用已更正/已保存的控制点重算；否则整体替换输入
        return reg_db.recompute_job(conn, job_id, body or None)
    finally:
        conn.close()


@app.get("/api/registration/jobs/{job_id}/result.geojson")
def download_registration_result(job_id: int) -> Response:
    """下载最近一次成功成果；重算失败时仍返回上一次成功成果。"""
    conn = get_conn()
    try:
        job = reg_db.get_job(conn, job_id)
    finally:
        conn.close()
    if not job["result"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": "尚无成功成果",
                "detail": "该配准作业从未计算成功，请先修正问题（退化/重叠/超出控制网）后重算",
                "issues": job["issues"],
            },
        )
    return Response(
        content=json.dumps(job["result"], ensure_ascii=False),
        media_type="application/geo+json",
        headers={
            "Content-Disposition": f'attachment; filename="registration_{job_id}_result.geojson"'
        },
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
