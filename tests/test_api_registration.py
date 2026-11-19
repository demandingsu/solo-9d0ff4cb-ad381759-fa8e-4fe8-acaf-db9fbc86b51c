"""图幅配准端到端 API 测试：成功配准、更正重算、失败后下载上次成果、问题定位。"""
import os
import tempfile

import pytest

# 独立数据库文件；DB_PATH 在 app.main 导入时固化，导入后再显式改写
_db_fd, _db_path = tempfile.mkstemp(suffix=".db", prefix="reg_api_")
os.close(_db_fd)
os.remove(_db_path)  # 确保从空库开始，避免与同进程其他模块的初始化互相干扰
os.environ["CADASTRE_DB"] = _db_path

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app.main import app  # noqa: E402

app_main.DB_PATH = _db_path

GROUND = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"parcel_id": "P1"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}},
        {"type": "Feature", "properties": {"parcel_id": "P2"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[10, 0], [20, 0], [20, 10], [10, 10], [10, 0]]]}},
    ],
}

GCPS = [
    {"pixel": [20, 20], "ground": [0, 0]},
    {"pixel": [220, 20], "ground": [10, 0]},
    {"pixel": [220, 220], "ground": [10, 10]},
    {"pixel": [20, 220], "ground": [0, 10]},
]


def fc(lines):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": f"L{i}"},
         "geometry": {"type": "LineString", "coordinates": line}}
        for i, line in enumerate(lines)
    ]}


CROSS_LINE = [[[120, 40], [20, 120], [40, 180]]]
BAD_LINE = [[[120, 120], [300, 120]]]  # 终点超出控制网


@pytest.fixture(scope="module", autouse=True)
def _clean_db():
    if os.path.exists(_db_path):
        os.remove(_db_path)
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post("/api/import", json=GROUND)
        assert r.status_code == 200, r.text
        yield c


def test_01_create_job_success_cross_region(client):
    body = {"base_version": 1, "gcps": GCPS, "polylines": fc(CROSS_LINE)}
    r = client.post("/api/registration/jobs", json=body)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "success" and job["issues"] == []
    assert job["result"]["type"] == "FeatureCollection"
    # 跨区分段后成果连续：只有一条 LineString
    feats = job["result"]["features"]
    assert len(feats) == 1 and feats[0]["geometry"]["type"] == "LineString"
    # 穿过分界线的交点 (20,120)->(0,5) 只出现一次，且共线冗余点已清理
    pts = [tuple(p) for p in feats[0]["geometry"]["coordinates"]]
    assert pts == [(5.0, 1.0), (0.0, 5.0), (1.0, 8.0)]


def test_02_preview_and_download_use_same_artifact(client):
    job = client.get("/api/registration/jobs/1").json()
    r = client.get("/api/registration/jobs/1/result.geojson")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    assert "attachment" in r.headers["content-disposition"]
    downloaded = r.json()
    # 下载内容与作业内保存的成果（预览所用）字节级一致
    assert downloaded == job["result"]


def test_03_recompute_with_failure_keeps_last_success(client):
    # 更正控制点 + 换成超网折线：重算失败
    bad_gcps = [dict(g) for g in GCPS]
    body = {"base_version": 1, "gcps": bad_gcps, "polylines": fc(BAD_LINE)}
    r = client.post("/api/registration/jobs/1/recompute", json=body)
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "failed"
    assert any(i["kind"] == "out_of_network" for i in job["issues"])
    # 问题带像素坐标几何，供前端定位
    issue = next(i for i in job["issues"] if i["kind"] == "out_of_network")
    assert issue["geometry"]["type"] == "LineString"
    # 关键：上次成功成果仍在，仍可下载同一份文件
    r2 = client.get("/api/registration/jobs/1/result.geojson")
    assert r2.status_code == 200
    kept = client.get("/api/registration/jobs/1").json()
    assert r2.json() == kept["result"]
    # 保留的正是第一次成功的跨区折线成果（共线冗余点已清理）
    coords = kept["result"]["features"][0]["geometry"]["coordinates"]
    assert coords == [[5.0, 1.0], [0.0, 5.0], [1.0, 8.0]]


def test_04_recompute_after_correction_succeeds_and_replaces(client):
    before = client.get("/api/registration/jobs/1/result.geojson").json()
    # 修正输入（去掉超网段），空请求体重算需先恢复有效输入：整体重提
    body = {"base_version": 1, "gcps": GCPS, "polylines": fc(CROSS_LINE)}
    r = client.post("/api/registration/jobs/1/recompute", json=body)
    assert r.status_code == 200 and r.json()["status"] == "success"
    after = client.get("/api/registration/jobs/1/result.geojson").json()
    assert after == before  # 同一输入，成果确定性一致


def test_05_degenerate_and_overlap_issues(client):
    # 源三角区退化
    deg = [dict(g) for g in GCPS]
    deg[2]["pixel"] = [120, 20]  # 与 G1/G2 共线
    r = client.post("/api/registration/jobs", json={
        "base_version": 1, "gcps": deg, "polylines": fc(CROSS_LINE)})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert any(i["kind"] == "degenerate_triangle" for i in r.json()["issues"])

    # 顺序错误导致三角区内部重叠（G3 跑到控制网外，两个像素三角区互插）
    ov = [dict(g) for g in GCPS]
    ov[2]["pixel"] = [200, 10]
    r = client.post("/api/registration/jobs", json={
        "base_version": 1, "gcps": ov, "polylines": fc(CROSS_LINE)})
    assert r.status_code == 200
    assert any(i["kind"] == "triangle_overlap" for i in r.json()["issues"])


def test_06_gcp_not_on_vertex_rejected(client):
    bad = [dict(g) for g in GCPS]
    bad[0]["ground"] = [3, 3]
    r = client.post("/api/registration/jobs", json={
        "base_version": 1, "gcps": bad, "polylines": fc(CROSS_LINE)})
    assert r.status_code == 200
    assert any(i["kind"] == "gcp_not_vertex" for i in r.json()["issues"])


def test_07_never_successful_job_download_409(client):
    r = client.post("/api/registration/jobs", json={
        "base_version": 1, "gcps": GCPS, "polylines": fc(BAD_LINE)})
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "failed" and r.json()["result"] is None
    d = client.get(f"/api/registration/jobs/{job_id}/result.geojson")
    assert d.status_code == 409
    assert d.json()["error"] == "尚无成功成果"


def test_08_invalid_inputs_and_missing_entities(client):
    # 控制点数量错误 -> 422
    r = client.post("/api/registration/jobs", json={
        "base_version": 1, "gcps": GCPS[:3], "polylines": fc(CROSS_LINE)})
    assert r.status_code == 422
    # 请求体不是合法 JSON -> 422（而非 500）
    r = client.post("/api/registration/jobs",
                    headers={"Content-Type": "application/json"}, content="{broken")
    assert r.status_code == 422 and "JSON" in r.json()["detail"]
    # 不存在的地籍版本 -> 422
    r = client.post("/api/registration/jobs", json={
        "base_version": 99, "gcps": GCPS, "polylines": fc(CROSS_LINE)})
    assert r.status_code == 422
    # 不存在的作业 -> 422
    assert client.get("/api/registration/jobs/999").status_code == 422
    # 作业列表
    jobs = client.get("/api/registration/jobs").json()["jobs"]
    assert len(jobs) >= 3
    assert {"job_id", "status", "has_result"} <= set(jobs[0].keys())
