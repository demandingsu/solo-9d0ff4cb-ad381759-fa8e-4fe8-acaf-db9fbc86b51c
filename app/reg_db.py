"""图幅配准作业持久层。

每个作业保存：所选地籍版本、四个控制点、最近一次计算状态与问题定位。
成果（result_json）只在计算成功时覆盖；更正控制点后重算失败时，
上一次成功成果原样保留，下载接口始终返回该份成果。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db as cadastre_db
from .geometry import GeoValidationError, PlanValidationError
from .registration import compute_registration, parse_gcps, parse_pixel_polylines

REG_SCHEMA = """
CREATE TABLE IF NOT EXISTS registration_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    base_version    INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    gcps_json       TEXT NOT NULL,
    polylines_json  TEXT NOT NULL,
    result_json     TEXT,
    issues_json     TEXT NOT NULL DEFAULT '[]',
    stats_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_reg_db(conn: sqlite3.Connection) -> None:
    conn.executescript(REG_SCHEMA)
    # 兼容在 stats_json 列引入前创建的数据库
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(registration_jobs)").fetchall()}
    if "stats_json" not in cols:
        conn.execute("ALTER TABLE registration_jobs ADD COLUMN stats_json TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "base_version": row["base_version"],
        "status": row["status"],
        "gcps": json.loads(row["gcps_json"]),
        "polylines": json.loads(row["polylines_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "issues": json.loads(row["issues_json"]),
        "stats": json.loads(row["stats_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ground_vertices_for_version(conn: sqlite3.Connection, version: int) -> set[tuple[float, float]]:
    """所选地籍版本上全部宗地环顶点集合（含闭合重复点，不影响集合判定）。"""
    if version < 1:
        raise PlanValidationError("地籍版本号必须 >= 1")
    cur = conn.execute("SELECT value FROM meta WHERE key = 'current_version'").fetchone()
    if cur is None or version > int(cur["value"]):
        raise PlanValidationError(f"地籍版本 v{version} 不存在")
    parcels = cadastre_db.snapshot_at(conn, version)
    vertices: set[tuple[float, float]] = set()
    for p in parcels:
        # snapshot 已返回 GeoJSON 几何；仅取外环顶点（本系统宗地均无洞）
        for x, y in p["geometry"]["coordinates"][0]:
            vertices.add((float(x), float(y)))
    return vertices


def _parse_request(req: dict) -> tuple[int, list[dict], list[dict]]:
    if not isinstance(req, dict):
        raise GeoValidationError("请求体必须是 JSON 对象")
    version = req.get("base_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise GeoValidationError("base_version 必须是 >= 1 的整数（所选地籍版本）")
    gcps = parse_gcps(req.get("gcps"))
    polylines = parse_pixel_polylines(req.get("polylines"))
    return version, gcps, polylines


def create_job(conn: sqlite3.Connection, req: dict) -> dict:
    """创建配准作业并执行首次整批计算。"""
    version, gcps, polylines = _parse_request(req)
    vertices = ground_vertices_for_version(conn, version)
    result = compute_registration(gcps, polylines, vertices)

    now = _now()
    cur = conn.execute(
        "INSERT INTO registration_jobs "
        "(base_version, status, gcps_json, polylines_json, result_json, issues_json, stats_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version,
            "success" if result.ok else "failed",
            json.dumps(gcps),
            json.dumps(polylines),
            json.dumps({"type": "FeatureCollection", "features": result.features}) if result.ok else None,
            json.dumps(result.issues, ensure_ascii=False),
            json.dumps(result.stats),
            now,
            now,
        ),
    )
    conn.commit()
    return get_job(conn, cur.lastrowid)


def recompute_job(conn: sqlite3.Connection, job_id: int, req: dict | None = None) -> dict:
    """更正控制点（或同时替换折线）后重算整批；失败时保留上次成功成果。"""
    row = conn.execute("SELECT * FROM registration_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise PlanValidationError(f"配准作业 {job_id} 不存在")
    job = _row_to_job(row)

    if req:
        version, gcps, polylines = _parse_request(req)
    else:
        version, gcps, polylines = job["base_version"], job["gcps"], job["polylines"]
    vertices = ground_vertices_for_version(conn, version)
    result = compute_registration(gcps, polylines, vertices)

    if result.ok:
        conn.execute(
            "UPDATE registration_jobs SET base_version=?, status='success', gcps_json=?, "
            "polylines_json=?, result_json=?, issues_json=?, stats_json=?, updated_at=? WHERE job_id=?",
            (
                version,
                json.dumps(gcps),
                json.dumps(polylines),
                json.dumps({"type": "FeatureCollection", "features": result.features}),
                json.dumps(result.issues, ensure_ascii=False),
                json.dumps(result.stats),
                _now(),
                job_id,
            ),
        )
    else:
        # 关键：不触碰 result_json，上一次成功成果保持可下载
        conn.execute(
            "UPDATE registration_jobs SET base_version=?, status='failed', gcps_json=?, "
            "polylines_json=?, issues_json=?, stats_json=?, updated_at=? WHERE job_id=?",
            (
                version,
                json.dumps(gcps),
                json.dumps(polylines),
                json.dumps(result.issues, ensure_ascii=False),
                json.dumps(result.stats),
                _now(),
                job_id,
            ),
        )
    conn.commit()
    return get_job(conn, job_id)


def list_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT job_id, base_version, status, created_at, updated_at, "
        "result_json IS NOT NULL AS has_result "
        "FROM registration_jobs ORDER BY job_id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute("SELECT * FROM registration_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise PlanValidationError(f"配准作业 {job_id} 不存在")
    return _row_to_job(row)
