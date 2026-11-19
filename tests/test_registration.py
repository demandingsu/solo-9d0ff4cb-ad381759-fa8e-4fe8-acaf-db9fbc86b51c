"""图幅配准几何单测：双三角分片仿射、跨区分段、连续性、去重与问题定位。"""
import math

import pytest
from shapely.geometry import LineString, shape

from app.registration import (
    Affine2D,
    compute_registration,
    parse_gcps,
    parse_pixel_polylines,
)
from app.geometry import GeoValidationError

# 底图：两宗共边矩形 P1(0..10) / P2(10..20)，环顶点作为合法登记顶点
GROUND_VERTICES = {
    (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0),
    (20.0, 0.0), (20.0, 10.0),
}

# 像素控制点（周边顺序）：源图在 x 方向被非线性拉伸，
# 单张全局仿射无法同时拟合四个点，必须分片处理。
#  G1=(20,20)->(0,0)  G2=(220,20)->(10,0)  G3=(220,220)->(10,10) G4=(20,220)->(0,10)
GCPS = [
    {"pixel": [20, 20], "ground": [0, 0]},
    {"pixel": [220, 20], "ground": [10, 0]},
    {"pixel": [220, 220], "ground": [10, 10]},
    {"pixel": [20, 220], "ground": [0, 10]},
]


def polyline_fc(lines, geom_type="LineString"):
    features = []
    for i, coords in enumerate(lines):
        if geom_type == "MultiLineString":
            geom = {"type": "MultiLineString", "coordinates": [coords]}
        else:
            geom = {"type": "LineString", "coordinates": coords}
        features.append(
            {"type": "Feature", "properties": {"id": f"L{i}"}, "geometry": geom}
        )
    return {"type": "FeatureCollection", "features": features}


def _compute(gcps, lines, vertices=GROUND_VERTICES, geom_type="LineString"):
    g = parse_gcps(gcps)
    p = parse_pixel_polylines(polyline_fc(lines, geom_type))
    return compute_registration(g, p, vertices)


def test_affine_identity_and_mapping():
    aff = Affine2D.solve([(0, 0), (1, 0), (0, 1)], [(5, 7), (6, 7), (5, 8)])
    assert aff.apply(2, 3) == (7, 10)


def test_cross_divider_polyline_is_continuous():
    # 从 A 区 (120,40) 穿过分界线 x=20 上的 (20,120) 到 B 区 (40,180)
    lines = [[[120, 40], [20, 120], [40, 180]]]
    res = _compute(GCPS, lines)
    assert res.ok, res.issues
    assert res.stats["output_line_count"] == 1  # 跨区分段后重新连成一条
    out = shape(res.features[0]["geometry"])
    assert out.geom_type == "LineString"
    coords = list(out.coords)
    assert res.features[0]["properties"]["source_id"] == "L0"
    # 交点 (20,120) 在两片仿射下结果一致（连续性），且只出现一次；
    # 共线冗余分界点被清理，成果恰好三个顶点
    flat = [c for pt in coords for c in pt]
    assert flat == pytest.approx([5, 1, 0, 5, 1, 8])
    # 端点映射：A 区 (120,40) -> x=5,y=1；B 区 (40,180) -> x=1,y=8
    assert coords[0] == pytest.approx((5, 1))
    assert coords[-1] == pytest.approx((1, 8))


def test_two_polylines_on_divider_emitted_once():
    # 两条折线都沿分界线 x=20 走完全相同的一段，且有一条还伸进 A 区
    lines = [
        [[20, 40], [20, 200]],
        [[120, 120], [20, 120], [20, 200]],
    ]
    res = _compute(GCPS, lines)
    assert res.ok, res.issues
    # 分界线（地面 x=0）上的公共段 (0,1)-(0,9) 在每个要素内部只出现一次
    for feat in res.features:
        geom = shape(feat["geometry"])
        xs = [round(x, 9) for x, _ in geom.coords]
        # 不含重复的相邻点
        assert len(geom.coords) == len(set(geom.coords))
        assert all(x >= -1e-9 for x in xs)
    ground_lines = [shape(f["geometry"]) for f in res.features]
    # 第一条成果应恰好是分界线线段 (0,1)-(0,9)
    div = LineString([(0, 1), (0, 9)])
    assert any(g.equals(div) for g in ground_lines)


def test_out_of_network_segment_reported_with_location():
    # 终点 (260,120) 在控制网（x∈[20,220]）之外
    lines = [[[120, 120], [260, 120]]]
    res = _compute(GCPS, lines)
    assert not res.ok
    kinds = [i["kind"] for i in res.issues]
    assert "out_of_network" in kinds
    issue = next(i for i in res.issues if i["kind"] == "out_of_network")
    # 问题位置以像素几何返回，便于在源图上标红
    g = shape(issue["geometry"])
    assert g.geom_type == "LineString"
    assert g.coords[0][0] == pytest.approx(220.0)
    assert g.coords[-1][0] == pytest.approx(260.0)
    # 失败不产出成果要素
    assert res.features == []


def test_degenerate_source_triangle():
    # G1/G2/G3 像素点共线（y 都为 20）
    bad = [
        {"pixel": [20, 20], "ground": [0, 0]},
        {"pixel": [220, 20], "ground": [10, 0]},
        {"pixel": [120, 20], "ground": [10, 10]},
        {"pixel": [20, 220], "ground": [0, 10]},
    ]
    res = _compute(bad, [[[60, 60], [100, 100]]])
    assert not res.ok
    assert any(i["kind"] == "degenerate_triangle" and i["space"] == "pixel" for i in res.issues)


def test_degenerate_ground_triangle():
    bad = [
        {"pixel": [20, 20], "ground": [0, 0]},
        {"pixel": [220, 20], "ground": [10, 0]},
        {"pixel": [220, 220], "ground": [10, 0]},   # 与 G2 登记点重合
        {"pixel": [20, 220], "ground": [0, 10]},
    ]
    res = _compute(bad, [[[60, 60], [100, 100]]])
    assert not res.ok
    assert any(i["kind"] == "degenerate_triangle" and i["space"] == "ground" for i in res.issues)


def test_internal_triangle_overlap_when_order_wrong():
    # 控制点不按周边顺序：G3 跑到控制网外（G2 一侧），两三角区出现面积重叠
    bad = [
        {"pixel": [20, 20], "ground": [0, 0]},
        {"pixel": [220, 20], "ground": [10, 0]},
        {"pixel": [200, 10], "ground": [5, 5]},
        {"pixel": [20, 220], "ground": [0, 10]},
    ]
    res = _compute(bad, [[[60, 60], [100, 100]]])
    assert not res.ok
    overlap = [i for i in res.issues if i["kind"] == "triangle_overlap"]
    assert overlap and overlap[0]["area"] > 0


def test_gcp_ground_point_not_a_parcel_vertex():
    bad = [dict(g, ground=[7, 3]) if i == 1 else dict(g) for i, g in enumerate(GCPS)]
    res = _compute(bad, [[[60, 60], [100, 100]]])
    assert not res.ok
    assert any(i["kind"] == "gcp_not_vertex" and i["gcp_index"] == 1 for i in res.issues)


def test_multiline_string_supported():
    lines = [[[120, 40], [20, 120]]]
    res = _compute(GCPS, lines, geom_type="MultiLineString")
    assert res.ok and res.stats["output_line_count"] == 1


def test_input_validation():
    with pytest.raises(GeoValidationError, match="恰好 4"):
        parse_gcps([])
    with pytest.raises(GeoValidationError, match="pixel"):
        # 点数够但缺少 pixel 字段
        parse_gcps([{"ground": [0, 0]}] * 4)
    with pytest.raises(GeoValidationError, match="有限数值"):
        parse_gcps([{"pixel": [0, float("nan")], "ground": [0, 0]}] * 4)
    bad_fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [1, 2]}}
    ]}
    with pytest.raises(GeoValidationError, match="LineString"):
        parse_pixel_polylines(bad_fc)


def test_control_points_map_exactly_to_ground_vertices():
    lines = [[[20, 20], [220, 20], [220, 220], [20, 220], [20, 20]]]
    res = _compute(GCPS, lines)
    assert res.ok, res.issues
    pts = set()
    for f in res.features:
        for x, y in shape(f["geometry"]).coords:
            pts.add((round(x, 9), round(y, 9)))
    for v in [(0, 0), (10, 0), (10, 10), (0, 10)]:
        assert v in pts
