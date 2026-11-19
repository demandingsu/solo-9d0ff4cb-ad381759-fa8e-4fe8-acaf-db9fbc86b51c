"""端到端 API 测试：导入、拆分、合并、违规预核验、竞争提交、失败回滚。"""
import json
import os
import tempfile
import threading

import pytest

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["CADASTRE_DB"] = _db_path

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _clean_db():
    if os.path.exists(_db_path):
        os.remove(_db_path)
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def fc(*items):
    feats = []
    for pid, ring in items:
        feats.append({
            "type": "Feature",
            "properties": {"parcel_id": pid},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return {"type": "FeatureCollection", "features": feats}


SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
LEFT = [[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]]
RIGHT = [[5, 0], [10, 0], [10, 10], [5, 10], [5, 0]]
P2_RING = [[10, 0], [20, 0], [20, 10], [10, 10], [10, 0]]


def change_req(version, retired, new_items, reason=None):
    return {
        "base_version": version,
        "retired_ids": retired,
        "new_parcels": [
            {"parcel_id": pid, "geometry": {"type": "Polygon", "coordinates": [ring]}}
            for pid, ring in new_items
        ],
        "reason": reason,
    }


def test_01_import_validation_errors(client):
    # 非整数坐标
    bad = fc(("B1", [[0, 0], [1.5, 0], [1.5, 2], [0, 2], [0, 0]]))
    r = client.post("/api/import", json=bad)
    assert r.status_code == 422 and "整数" in r.json()["detail"]

    # 带洞
    hole = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"parcel_id": "H1"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
            ]},
        }],
    }
    r = client.post("/api/import", json=hole)
    assert r.status_code == 422 and "洞" in r.json()["detail"]

    # 初始宗地重叠（共边的 P1/P2 不重叠，这里造一个侵入 P1 的面）
    overlap = fc(("P1", SQUARE), ("PX", [[9, 0], [12, 0], [12, 10], [9, 10], [9, 0]]))
    r = client.post("/api/import", json=overlap)
    assert r.status_code == 422 and "重叠" in r.json()["detail"]


def test_02_import_ok_and_no_duplicate(client):
    payload = fc(("P1", SQUARE), ("P2", P2_RING))
    r = client.post("/api/import", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["new_version"] == 1

    # 重复导入被拒，数据保持原状
    r2 = client.post("/api/import", json=payload)
    assert r2.status_code == 409
    state = client.get("/api/state").json()
    assert state["current_version"] == 1 and state["parcel_count"] == 2


def test_03_plan_split_with_gap_and_encroachment(client):
    # 问题面：只盖 P1 的 0..7，造成缺口（7..10）且侵占 P2（10..7 段在 P2 内）
    req = change_req(1, ["P1"], [("P1X", [[0, 0], [7, 0], [7, 10], [0, 10], [0, 0]])])
    r = client.post("/api/changes/plan", json=req)
    assert r.status_code == 200
    rep = r.json()
    assert rep["valid"] is False
    assert len(rep["gaps"]) == 1 and rep["gaps"][0]["area"] == 30
    # 新面 x 只到 7，未越过共边 x=10，因此无侵占、无越界，仅缺口
    assert rep["excess"] == []
    assert rep["encroachments"] == []

    # 再造一个真正侵占 P2 的面：新面 x 到 12
    req2 = change_req(1, ["P1"], [("P1Y", [[0, 0], [12, 0], [12, 10], [0, 10], [0, 0]])])
    rep2 = client.post("/api/changes/plan", json=req2).json()
    assert rep2["valid"] is False
    assert len(rep2["encroachments"]) == 1 and rep2["encroachments"][0]["area"] == 20
    assert rep2["excess"] and rep2["excess"][0]["area"] == 20


def test_04_plan_does_not_mutate(client):
    state = client.get("/api/state").json()
    assert state["current_version"] == 1
    assert sorted(p["parcel_id"] for p in state["parcels"]) == ["P1", "P2"]


def test_05_commit_invalid_plan_rejected(client):
    req = change_req(1, ["P1"], [("P1X", [[0, 0], [7, 0], [7, 10], [0, 10], [0, 0]])])
    r = client.post("/api/changes/commit", json=req)
    assert r.status_code == 422
    state = client.get("/api/state").json()
    assert state["current_version"] == 1 and state["parcel_count"] == 2


def test_06_split_commit(client):
    req = change_req(1, ["P1"], [("P1A", LEFT), ("P1B", RIGHT)], reason="宗地分割")
    # 预核验通过
    plan = client.post("/api/changes/plan", json=req).json()
    assert plan["valid"] is True and plan["gaps"] == [] and plan["encroachments"] == []

    r = client.post("/api/changes/commit", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["new_version"] == 2 and body["retired_ids"] == ["P1"]

    state = client.get("/api/state").json()
    assert state["current_version"] == 2
    assert sorted(p["parcel_id"] for p in state["parcels"]) == ["P1A", "P1B", "P2"]

    # 新面互叠会被拦（P1A 范围 0..5，两个新面在 0..5 重叠）
    dup = change_req(2, ["P1A"], [
        ("P1A1", [[0, 0], [6, 0], [6, 10], [0, 10], [0, 0]]),
        ("P1A2", [[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]]),
    ])
    rep = client.post("/api/changes/plan", json=dup).json()
    assert rep["valid"] is False and len(rep["internal_overlaps"]) == 1


def test_07_merge_commit(client):
    req = change_req(2, ["P1A", "P1B"], [("P1C", SQUARE)], reason="宗地合并")
    plan = client.post("/api/changes/plan", json=req).json()
    assert plan["valid"] is True
    r = client.post("/api/changes/commit", json=req)
    assert r.status_code == 200 and r.json()["new_version"] == 3

    state = client.get("/api/state").json()
    assert sorted(p["parcel_id"] for p in state["parcels"]) == ["P1C", "P2"]


def test_08_history_and_snapshots(client):
    # P1 的历史：v1 为现势行，v2 为退休行（append-only，旧行保留）
    h = client.get("/api/history/parcel/P1").json()
    assert [(v["version"], v["status"]) for v in h["versions"]] == [(1, "current"), (2, "retired")]
    assert h["versions"][1]["retired_txn_id"] == 2

    # 不存在的宗地
    assert client.get("/api/history/parcel/NOPE").status_code == 422

    # v1 快照：P1 + P2
    snap1 = client.get("/api/history/version/1").json()
    assert sorted(p["parcel_id"] for p in snap1["parcels"]) == ["P1", "P2"]
    # v2 快照：P1A P1B P2
    snap2 = client.get("/api/history/version/2").json()
    assert sorted(p["parcel_id"] for p in snap2["parcels"]) == ["P1A", "P1B", "P2"]
    # v3 快照：P1C P2
    snap3 = client.get("/api/history/version/3").json()
    assert sorted(p["parcel_id"] for p in snap3["parcels"]) == ["P1C", "P2"]

    txns = client.get("/api/txns").json()["txns"]
    assert [t["txn_id"] for t in txns] == [1, 2, 3]
    detail = client.get("/api/txns/2").json()
    roles = {(i["role"], i["parcel_id"]) for i in detail["items"]}
    assert ("retired", "P1") in roles and ("new", "P1A") in roles and ("new", "P1B") in roles


def test_09_competing_commits_only_one_wins(client):
    ver = client.get("/api/state").json()["current_version"]
    assert ver == 3
    # 两笔都基于 v3：把 P1C 再拆两半（使用全新宗地号，避免与历史号段冲突）
    req_a = change_req(3, ["P1C"], [("R1", LEFT), ("R2", RIGHT)])
    req_b = change_req(3, ["P1C"], [
        ("Q1", [[0, 0], [4, 0], [4, 10], [0, 10], [0, 0]]),
        ("Q2", [[4, 0], [10, 0], [10, 10], [4, 10], [4, 0]]),
    ])

    results = {}
    barrier = threading.Barrier(2)

    def worker(key, req):
        barrier.wait()
        with TestClient(app) as local:
            results[key] = local.post("/api/changes/commit", json=req).status_code

    t1 = threading.Thread(target=worker, args=("a", req_a))
    t2 = threading.Thread(target=worker, args=("b", req_b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == [200, 409]
    state = client.get("/api/state").json()
    assert state["current_version"] == 4  # 只有一笔生效
    ids = sorted(p["parcel_id"] for p in state["parcels"])
    assert ids in (["P2", "Q1", "Q2"], ["P2", "R1", "R2"])

    # 失败方用旧版本号重试仍然 409；带新版本重新核验后才能成功
    stale = client.post("/api/changes/commit", json=req_a if results["b"] == 200 else req_b)
    assert stale.status_code == 409


def test_10_failed_commit_keeps_state_intact(client):
    before = client.get("/api/state").json()
    # 退出不存在的宗地
    bad = change_req(before["current_version"], ["GHOST"], [("Z1", SQUARE)])
    r = client.post("/api/changes/commit", json=bad)
    assert r.status_code == 422
    after = client.get("/api/state").json()
    assert after["current_version"] == before["current_version"]
    assert after["parcels"] == before["parcels"]

    # 新宗地号与现势冲突
    existing_id = before["parcels"][0]["parcel_id"]
    bad2 = change_req(before["current_version"], ["P2"],
                      [(existing_id, P2_RING)])
    r2 = client.post("/api/changes/commit", json=bad2)
    assert r2.status_code == 422
    assert client.get("/api/state").json()["current_version"] == before["current_version"]
