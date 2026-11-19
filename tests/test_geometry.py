"""几何规则的边界用例：共边/点接触允许、自相交拒绝、多要素合并、退化面拒绝。"""
import pytest

from app.geometry import (
    EPS,
    GeoValidationError,
    ensure_no_overlap,
    parse_feature_collection,
    parse_polygon,
    plan_change,
)
from shapely.geometry import shape


def ring(coords):
    coords = [list(c) for c in coords]
    return coords if coords[-1] == coords[0] else coords + [coords[0]]


def poly(coords):
    return shape({"type": "Polygon", "coordinates": [ring(coords)]})


def test_shared_edge_is_not_overlap():
    a = poly([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = poly([(10, 0), (20, 0), (20, 10), (10, 10)])
    assert ensure_no_overlap([("A", a), ("B", b)], label="t") == []
    # 点接触同样不算面积重叠
    c = poly([(10, 10), (15, 10), (15, 15), (10, 15)])
    assert ensure_no_overlap([("A", a), ("C", c)], label="t") == []


def test_tiny_area_overlap_detected():
    a = poly([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = poly([(9, 0), (12, 0), (12, 10), (9, 10)])  # 侵入 1 个单位宽
    overlaps = ensure_no_overlap([("A", a), ("B", b)], label="t")
    assert len(overlaps) == 1 and overlaps[0]["area"] == pytest.approx(10.0)


def test_self_intersecting_polygon_rejected():
    bowtie = {"type": "Polygon",
              "coordinates": [ring([(0, 0), (4, 4), (4, 0), (0, 4)])]}
    with pytest.raises(GeoValidationError, match="几何无效"):
        parse_polygon(bowtie, label="X")


def test_zero_area_polygon_rejected():
    degenerate = {"type": "Polygon",
                  "coordinates": [ring([(0, 0), (5, 0), (5, 0), (0, 0)])]}
    with pytest.raises(GeoValidationError):
        parse_polygon(degenerate, label="X")


def test_non_integer_and_hole_rejected():
    with pytest.raises(GeoValidationError, match="整数"):
        parse_polygon({"type": "Polygon", "coordinates": [ring([(0, 0), (1.2, 0), (1.2, 2), (0, 2)])]})
    with pytest.raises(GeoValidationError, match="洞"):
        parse_polygon({"type": "Polygon", "coordinates": [
            ring([(0, 0), (10, 0), (10, 10), (0, 10)]),
            ring([(4, 4), (6, 4), (6, 6), (4, 6)]),
        ]})


def test_plan_exact_cover_with_three_successors():
    old = [("O", poly([(0, 0), (9, 0), (9, 9), (0, 9)]))]
    new = [
        ("N1", poly([(0, 0), (3, 0), (3, 9), (0, 9)])),
        ("N2", poly([(3, 0), (6, 0), (6, 9), (3, 9)])),
        ("N3", poly([(6, 0), (9, 0), (9, 9), (6, 9)])),
    ]
    report = plan_change(old, new, [])
    assert report.valid and report.gaps == [] and report.excess == []
    assert report.retired_union_area == report.new_union_area == 81


def test_plan_merge_two_into_one():
    old = [
        ("A", poly([(0, 0), (5, 0), (5, 8), (0, 8)])),
        ("B", poly([(5, 0), (10, 0), (10, 8), (5, 8)])),
    ]
    new = [("M", poly([(0, 0), (10, 0), (10, 8), (0, 8)]))]
    report = plan_change(old, new, [])
    assert report.valid


def test_plan_encroachment_on_surviving():
    old = [("O", poly([(0, 0), (5, 0), (5, 5), (0, 5)]))]
    survivor = [("S", poly([(5, 0), (10, 0), (10, 5), (5, 5)]))]
    # 新面整体向右多吃 2 个单位
    new = [("N", poly([(0, 0), (7, 0), (7, 5), (0, 5)]))]
    report = plan_change(old, new, survivor)
    assert not report.valid
    assert len(report.encroachments) == 1
    assert report.encroachments[0]["area"] == pytest.approx(10.0)
    assert report.excess[0]["area"] == pytest.approx(10.0)


def test_parse_feature_collection_duplicate_id():
    payload = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"parcel_id": "X"},
         "geometry": {"type": "Polygon", "coordinates": [ring([(0, 0), (1, 0), (1, 1), (0, 1)])]}},
        {"type": "Feature", "properties": {"parcel_id": "X"},
         "geometry": {"type": "Polygon", "coordinates": [ring([(2, 0), (3, 0), (3, 1), (2, 1)])]}},
    ]}
    with pytest.raises(GeoValidationError, match="重复"):
        parse_feature_collection(payload)
