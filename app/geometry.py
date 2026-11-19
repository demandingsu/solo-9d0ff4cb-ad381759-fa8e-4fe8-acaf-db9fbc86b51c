"""几何校验：导入校验与变更预核验（缺口 / 侵占 / 重叠）。

约束（需求）：
- 单一平面坐标系、整数坐标、无洞 Polygon；
- 初始宗地之间不得面积重叠（共边允许）；
- 变更：新面之间不得面积重叠，不得侵占未退出宗地，
  新面并集须恰好覆盖退出旧面并集（共边允许）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import explain_validity

# 整数坐标下面积均为 0.5 的整数倍，1e-9 足够区分真实误差与浮点噪声
EPS = 1e-9


class GeoValidationError(ValueError):
    """输入数据本身不合法（坐标、环、字段等）。"""


class PlanValidationError(ValueError):
    """变更请求的业务前提不成立（宗地号不存在等）。"""


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def feature_geometry(f: dict) -> dict:
    if f.get("type") != "Feature":
        raise GeoValidationError("集合中的元素必须是 GeoJSON Feature")
    geom = f.get("geometry")
    if not isinstance(geom, dict):
        raise GeoValidationError("Feature 缺少 geometry")
    return geom


def parse_polygon(geom: dict, *, label: str = "") -> BaseGeometry:
    """解析并校验单个无洞、整数坐标的 Polygon。"""
    if geom.get("type") != "Polygon":
        raise GeoValidationError(
            f"{label} 几何类型必须是 Polygon（不支持 MultiPolygon），实际为 {geom.get('type')}"
        )
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or not coords:
        raise GeoValidationError(f"{label} Polygon 坐标环为空")
    if len(coords) != 1:
        raise GeoValidationError(f"{label} Polygon 不允许带洞（存在内环）")
    ring = coords[0]
    if len(ring) < 4:
        raise GeoValidationError(f"{label} 坐标环至少需要 4 个点（含闭合点）")
    for pt in ring:
        if not isinstance(pt, list) or len(pt) != 2:
            raise GeoValidationError(f"{label} 仅支持二维坐标点 [x, y]")
        x, y = pt
        if not (_is_int(x) and _is_int(y)):
            raise GeoValidationError(f"{label} 坐标必须为整数，发现 ({x!r}, {y!r})")
    if ring[0] != ring[-1]:
        raise GeoValidationError(f"{label} 坐标环必须首尾闭合")
    g = shape(geom)
    if not g.is_valid:
        raise GeoValidationError(f"{label} 几何无效：{explain_validity(g)}")
    if g.area <= EPS:
        raise GeoValidationError(f"{label} 多边形面积为 0")
    return g


def parse_feature_collection(payload: Any) -> list[tuple[str, BaseGeometry, dict]]:
    """解析导入数据，返回 (parcel_id, polygon, 原始feature) 列表。"""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise GeoValidationError("顶层必须是 GeoJSON FeatureCollection")
    feats = payload.get("features")
    if not isinstance(feats, list) or not feats:
        raise GeoValidationError("features 为空")
    out: list[tuple[str, BaseGeometry, dict]] = []
    seen: set[str] = set()
    for i, f in enumerate(feats):
        props = f.get("properties") or {}
        pid = props.get("parcel_id")
        if pid is None or str(pid).strip() == "":
            raise GeoValidationError(f"第 {i + 1} 个要素缺少 properties.parcel_id（宗地号）")
        pid = str(pid)
        if pid in seen:
            raise GeoValidationError(f"宗地号重复：{pid}")
        seen.add(pid)
        geom = parse_polygon(feature_geometry(f), label=f"宗地 {pid}")
        out.append((pid, geom, f))
    return out


def ensure_no_overlap(
    items: list[tuple[str, BaseGeometry]], *, label: str
) -> list[dict]:
    """检查同组多边形之间不存在面积重叠；返回重叠明细（空列表即通过）。"""
    overlaps: list[dict] = []
    for i in range(len(items)):
        id_a, g_a = items[i]
        for j in range(i + 1, len(items)):
            id_b, g_b = items[j]
            inter = g_a.intersection(g_b)
            if not inter.is_empty and inter.area > EPS:
                overlaps.append(
                    {
                        "kind": "internal_overlap",
                        "a": id_a,
                        "b": id_b,
                        "geometry": mapping(inter),
                        "area": inter.area,
                        "message": f"{label} {id_a} 与 {id_b} 面积重叠",
                    }
                )
    return overlaps


@dataclass
class ChangeReport:
    valid: bool
    errors: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)          # 旧面未被覆盖的缺口
    encroachments: list[dict] = field(default_factory=list)  # 侵占未退出宗地
    excess: list[dict] = field(default_factory=list)         # 超出退出旧面范围
    internal_overlaps: list[dict] = field(default_factory=list)
    retired_union_area: float = 0.0
    new_union_area: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "gaps": self.gaps,
            "encroachments": self.encroachments,
            "excess": self.excess,
            "internal_overlaps": self.internal_overlaps,
            "retired_union_area": self.retired_union_area,
            "new_union_area": self.new_union_area,
        }


def _dump_parts(geom: BaseGeometry, kind: str, message: str) -> list[dict]:
    if geom.is_empty or geom.area <= EPS:
        return []
    parts = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
    return [
        {
            "kind": kind,
            "message": message,
            "area": g.area,
            "geometry": mapping(g),
        }
        for g in parts
        if not g.is_empty and g.area > EPS
    ]


def plan_change(
    retired: list[tuple[str, BaseGeometry]],
    new_items: list[tuple[str, BaseGeometry]],
    surviving: list[tuple[str, BaseGeometry]],
) -> ChangeReport:
    """预核验一次分割/合并变更，纯计算、不落库。"""
    report = ChangeReport(valid=False)
    errors: list[str] = []

    report.internal_overlaps = ensure_no_overlap(new_items, label="新宗地")
    if report.internal_overlaps:
        errors.append("新宗地之间存在面积重叠")

    old_union = unary_union([g for _, g in retired])
    new_union = unary_union([g for _, g in new_items])
    report.retired_union_area = old_union.area
    report.new_union_area = new_union.area

    # 缺口：退出旧面中没有被新面覆盖的部分
    gap_geom = old_union.difference(new_union)
    report.gaps = _dump_parts(gap_geom, "gap", "缺口：退出旧面未被新面覆盖")
    if report.gaps:
        errors.append("新面未恰好覆盖退出旧面：存在缺口")

    # 超出：新面落在退出旧面并集之外的部分
    excess_geom = new_union.difference(old_union)
    report.excess = _dump_parts(excess_geom, "excess", "超出：新面越出退出旧面范围")
    if report.excess:
        errors.append("新面未恰好覆盖退出旧面：存在越界")

    # 侵占：新面与未退出宗地的面积相交
    for nid, ng in new_items:
        for sid, sg in surviving:
            inter = ng.intersection(sg)
            if not inter.is_empty and inter.area > EPS:
                report.encroachments.append(
                    {
                        "kind": "encroachment",
                        "new_parcel": nid,
                        "surviving_parcel": sid,
                        "geometry": mapping(inter),
                        "area": inter.area,
                        "message": f"新宗地 {nid} 侵占未退出宗地 {sid}",
                    }
                )
    if report.encroachments:
        errors.append("新面侵占了未退出的宗地")

    report.errors = errors
    report.valid = not errors
    return report
