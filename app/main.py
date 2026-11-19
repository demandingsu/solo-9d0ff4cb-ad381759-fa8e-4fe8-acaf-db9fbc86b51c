"""地籍变更 Web 应用：导入、预核验、确认提交、现势/历史/变更记录查询。"""
from __future__ import annotations

import os
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .geometry import GeoValidationError, PlanValidationError

DB_PATH = os.environ.get("CADASTRE_DB", "/data/cadastre.db")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def get_conn() -> db.sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_db(conn)
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


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
