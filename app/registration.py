"""图幅配准：四控制点双三角分片仿射配准的纯几何计算。

业务规则（需求）：
- 四个像素控制点按周边顺序录入，各自对应一个登记顶点（底图宗地环上的顶点）；
- 沿第 1、3 点连线（分界线）把控制网分成两个三角区：
  三角区 A = (G1, G2, G3)，三角区 B = (G1, G4, G3)，各用三对点求解仿射变换；
- 像素折线跨区分段：与控制网求交后按三角区分片变换，分界线两侧变换结果连续，
  落在分界线上的线段只输出一次；
- 问题定位（均为致命问题，本次计算不产出新成果）：
  1. 源图（像素）或底图（登记坐标）三角区退化（面积为 0，三点共线/重合）；
  2. 两个三角区内部面积重叠（控制点未按周边顺序录入等）；
  3. 折线线段超出控制网（位于两三角区并集之外）；
  4. 登记控制点不在所选地籍版本的宗地顶点集合内。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from shapely.affinity import affine_transform
from shapely.geometry import LineString, Polygon, mapping
from shapely.ops import linemerge, transform as shp_transform
from shapely.ops import unary_union

from .geometry import GeoValidationError

# 浮点容差：面积/长度判零阈值，输出坐标统一保留 9 位小数，
# 使分界线两侧分别变换得到的同一交点坐标取整后严格相等。
EPS = 1e-9
ROUND_NDIGITS = 9


@dataclass
class Affine2D:
    """二维仿射变换：x' = a*x + b*y + xoff；y' = d*x + e*y + yoff。"""

    a: float
    b: float
    xoff: float
    d: float
    e: float
    yoff: float

    @classmethod
    def solve(cls, src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> "Affine2D":
        """由三对对应点求解仿射变换；源三点共线时调用方应先拦截退化。"""
        (x0, y0), (x1, y1), (x2, y2) = src
        (u0, v0), (u1, v1), (u2, v2) = dst
        det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(det) <= EPS:
            raise GeoValidationError("仿射求解失败：源三角区退化")
        # 线性部分 L = Mt · Ms^-1
        a = ((u1 - u0) * (y2 - y0) - (u2 - u0) * (y1 - y0)) / det
        b = ((u2 - u0) * (x1 - x0) - (u1 - u0) * (x2 - x0)) / det
        d = ((v1 - v0) * (y2 - y0) - (v2 - v0) * (y1 - y0)) / det
        e = ((v2 - v0) * (x1 - x0) - (v1 - v0) * (x2 - x0)) / det
        return cls(
            a=a,
            b=b,
            xoff=u0 - (a * x0 + b * y0),
            d=d,
            e=e,
            yoff=v0 - (d * x0 + e * y0),
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.a * x + self.b * y + self.xoff,
            self.d * x + self.e * y + self.yoff,
        )

    def shapely_matrix(self) -> list[float]:
        # shapely.affinity.affine_transform 参数顺序 [a, b, d, e, xoff, yoff]
        return [self.a, self.b, self.d, self.e, self.xoff, self.yoff]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _point(v: Any, label: str) -> tuple[float, float]:
    if not isinstance(v, list) or len(v) != 2 or not all(_is_num(c) for c in v):
        raise GeoValidationError(f"{label} 必须是两个有限数值构成的 [x, y] 点")
    return float(v[0]), float(v[1])


def parse_gcps(raw: Any) -> list[dict]:
    """校验并解析四个控制点：[{pixel:[x,y], ground:[x,y]}, ...]（按周边顺序）。"""
    if not isinstance(raw, list) or len(raw) != 4:
        raise GeoValidationError("控制点 gcps 必须是恰好 4 个点的数组（按周边顺序录入）")
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "pixel" not in item or "ground" not in item:
            raise GeoValidationError(f"控制点 {i + 1} 必须包含 pixel 与 ground 两个字段")
        out.append(
            {
                "pixel": list(_point(item["pixel"], f"控制点 {i + 1} 的像素坐标")),
                "ground": list(_point(item["ground"], f"控制点 {i + 1} 的登记坐标")),
            }
        )
    return out


def parse_pixel_polylines(payload: Any) -> list[dict]:
    """校验像素折线 FeatureCollection，仅允许 LineString / MultiLineString。"""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise GeoValidationError("像素折线顶层必须是 GeoJSON FeatureCollection")
    feats = payload.get("features")
    if not isinstance(feats, list) or not feats:
        raise GeoValidationError("像素折线 features 为空")
    out: list[dict] = []
    for i, f in enumerate(feats):
        if not isinstance(f, dict) or f.get("type") != "Feature":
            raise GeoValidationError(f"折线 {i + 1} 必须是 GeoJSON Feature")
        geom = f.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") not in ("LineString", "MultiLineString"):
            raise GeoValidationError(
                f"折线 {i + 1} 几何类型必须是 LineString 或 MultiLineString"
            )
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or not coords:
            raise GeoValidationError(f"折线 {i + 1} 坐标为空")
        rings = coords if geom["type"] == "MultiLineString" else [coords]
        for j, line in enumerate(rings):
            if len(line) < 2:
                raise GeoValidationError(f"折线 {i + 1} 第 {j + 1} 段至少需要 2 个点")
            for pt in line:
                if not isinstance(pt, list) or len(pt) != 2 or not all(_is_num(c) for c in pt):
                    raise GeoValidationError(
                        f"折线 {i + 1} 存在非法坐标点（须为两个有限数值）"
                    )
        props = f.get("properties") or {}
        out.append(
            {
                "index": i,
                "source_id": str(props.get("id") if props.get("id") is not None else props.get("name", i + 1)),
                "geometry": geom,
            }
        )
    return out


def _triangle_area(points: list[tuple[float, float]]) -> float:
    (x0, y0), (x1, y1), (x2, y2) = points
    return abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / 2.0


def _line_parts(geom: Any) -> list[LineString]:
    """从线/面交集中抽出非退化 LineString 列表（忽略 Point 等接触结果）。"""
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length > EPS else []
    if geom.geom_type == "MultiLineString":
        return [g for g in geom.geoms if g.length > EPS]
    parts: list[LineString] = []
    if hasattr(geom, "geoms"):
        for g in geom.geoms:
            parts.extend(_line_parts(g))
    return parts


# 共线清理容差（相邻边夹角正弦值）：两片仿射在分界点的结果可能相差
# 1e-13 量级，归一化后夹角正弦约 1e-10；1e-8 既能吃掉浮点噪声，又不会误删真实折点。
COLLINEAR_SIN_TOL = 1e-8


def _clean_line(geom: Any) -> Any:
    """去掉折线中近似共线的中间顶点（保留首尾与真实拐点）。"""
    if geom.geom_type != "LineString":
        return geom
    pts = list(geom.coords)
    if len(pts) <= 2:
        return geom
    kept = [pts[0]]
    for p in pts[1:-1]:
        a = kept[-1]
        # 先看与已保留最后一点是否重合
        if abs(p[0] - a[0]) <= EPS and abs(p[1] - a[1]) <= EPS:
            continue
        kept.append(p)
    kept.append(pts[-1])
    # 第二遍：依据相邻边夹角正弦（归一化叉积）剔除共线中点
    out: list[tuple[float, float]] = []
    for i, p in enumerate(kept):
        if 0 < i < len(kept) - 1:
            a, c = kept[i - 1], kept[i + 1]
            v1x, v1y = p[0] - a[0], p[1] - a[1]
            v2x, v2y = c[0] - p[0], c[1] - p[1]
            cross = v1x * v2y - v1y * v2x
            l1 = math.hypot(v1x, v1y)
            l2 = math.hypot(v2x, v2y)
            sin_angle = abs(cross) / (l1 * l2) if l1 > EPS and l2 > EPS else 0.0
            if sin_angle <= COLLINEAR_SIN_TOL:
                continue
        out.append(p)
    if len(out) < 2:
        return geom
    return LineString(out)


def _round_geom(geom: Any) -> Any:
    def _r(x: float, y: float, z: Any = None) -> tuple[float, ...]:
        rx, ry = round(x, ROUND_NDIGITS), round(y, ROUND_NDIGITS)
        return (rx, ry) if z is None else (rx, ry, z)

    return shp_transform(_r, geom)


def _issue(kind: str, message: str, **extra: Any) -> dict:
    return {"kind": kind, "message": message, **extra}


@dataclass
class RegistrationResult:
    ok: bool
    issues: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)  # 登记坐标成果要素
    stats: dict = field(default_factory=dict)


def compute_registration(
    gcps: list[dict],
    polylines: list[dict],
    ground_vertices: set[tuple[float, float]],
) -> RegistrationResult:
    """执行整批配准计算。纯函数，不落库。

    成功时 features 为登记坐标 LineString 要素；任一问题存在即 ok=False，
    不产出 features（由调用方保留上一次成功成果）。
    """
    issues: list[dict] = []
    px = [tuple(g["pixel"]) for g in gcps]
    gd = [tuple(g["ground"]) for g in gcps]

    # 1) 登记控制点必须是所选地籍版本上的宗地顶点
    for i, g in enumerate(gd):
        if g not in ground_vertices:
            issues.append(
                _issue(
                    "gcp_not_vertex",
                    f"控制点 {i + 1} 的登记顶点 ({g[0]}, {g[1]}) 不在所选地籍版本的宗地顶点上",
                    gcp_index=i,
                    ground=list(g),
                )
            )

    # 2) 三角区退化检查（源图 / 底图分别检查）
    tri_idx = {"A": (0, 1, 2), "B": (0, 3, 2)}
    tri_points: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for name, (i1, i2, i3) in tri_idx.items():
        src_pts = [px[i1], px[i2], px[i3]]
        dst_pts = [gd[i1], gd[i2], gd[i3]]
        tri_points[name] = {"pixel": src_pts, "ground": dst_pts}
        if _triangle_area(src_pts) <= EPS:
            issues.append(
                _issue(
                    "degenerate_triangle",
                    f"源图三角区 {name}（控制点 {i1 + 1}/{i2 + 1}/{i3 + 1}）退化：三点共线或重合",
                    triangle=name,
                    space="pixel",
                    points=[list(p) for p in src_pts],
                )
            )
        if _triangle_area(dst_pts) <= EPS:
            issues.append(
                _issue(
                    "degenerate_triangle",
                    f"底图三角区 {name}（登记顶点 {i1 + 1}/{i2 + 1}/{i3 + 1}）退化：三点共线或重合",
                    triangle=name,
                    space="ground",
                    points=[list(p) for p in dst_pts],
                )
            )

    # 3) 三角区内部重叠检查（控制点未按周边顺序时两三角形相互穿插）
    tri_polys: dict[str, dict[str, Polygon]] = {}
    for name in tri_idx:
        tri_polys[name] = {
            "pixel": Polygon(tri_points[name]["pixel"]),
            "ground": Polygon(tri_points[name]["ground"]),
        }
    for space in ("pixel", "ground"):
        pa, pb = tri_polys["A"][space], tri_polys["B"][space]
        if pa.area <= EPS or pb.area <= EPS:
            continue  # 退化已另行报告，跳过重叠判定
        inter = pa.intersection(pb)
        if not inter.is_empty and inter.area > EPS:
            issues.append(
                _issue(
                    "triangle_overlap",
                    f"{'源图' if space == 'pixel' else '底图'}两个三角区内部重叠，"
                    "请检查控制点是否按周边顺序录入",
                    space=space,
                    area=inter.area,
                    geometry=mapping(inter),
                )
            )

    if issues:
        # 控制网本身不成立，无法进行分片变换
        return RegistrationResult(ok=False, issues=issues, stats={"polylines": len(polylines)})

    # 4) 求解两片仿射变换
    aff_a = Affine2D.solve(tri_points["A"]["pixel"], tri_points["A"]["ground"])
    aff_b = Affine2D.solve(tri_points["B"]["pixel"], tri_points["B"]["ground"])
    net_poly = unary_union([tri_polys["A"]["pixel"], tri_polys["B"]["pixel"]])

    out_features: list[dict] = []
    out_count = 0
    for feat in polylines:
        idx = feat["index"]
        src_geom = shape_like(feat["geometry"])
        lines = (
            list(src_geom.geoms)
            if src_geom.geom_type == "MultiLineString"
            else [src_geom]
        )
        pieces: list[Any] = []
        for j, line in enumerate(lines):
            # 4.1 超出控制网的部分（像素坐标定位）
            outside = _line_parts(line.difference(net_poly))
            for k, part in enumerate(outside):
                issues.append(
                    _issue(
                        "out_of_network",
                        f"折线 {idx + 1} 第 {j + 1} 段存在超出控制网的部分（第 {k + 1} 截，"
                        f"像素长度 {part.length:.3f}）",
                        polyline_index=idx,
                        part_index=j,
                        length=part.length,
                        geometry=mapping(part),
                    )
                )
            inside = _line_parts(line.intersection(net_poly))
            for sub in inside:
                pa_parts = _line_parts(sub.intersection(tri_polys["A"]["pixel"]))
                pb_parts = _line_parts(sub.intersection(tri_polys["B"]["pixel"]))
                for p in pa_parts:
                    pieces.append(_round_geom(affine_transform(p, aff_a.shapely_matrix())))
                for p in pb_parts:
                    pieces.append(_round_geom(affine_transform(p, aff_b.shapely_matrix())))
        if not pieces:
            continue
        # 同源折线先 union 去掉分界线上被两侧各产出一次的重复线段，
        # 再 linemerge 把端点相接的分段重新接成连续折线（跨区连续性）。
        # shapely 2.x 的 linemerge 不接受裸 LineString，统一以列表传入；
        # 最后 _clean_line 去掉两片在分界点处因浮点噪声留下的共线冗余顶点。
        unioned = unary_union(pieces)
        merge_input = list(unioned.geoms) if unioned.geom_type == "MultiLineString" else [unioned]
        merged = _round_geom(linemerge(merge_input))
        merged_lines = [_clean_line(g) for g in _line_parts(merged)]
        for ml in merged_lines:
            out_count += 1
            out_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "source_index": idx,
                        "source_id": feat["source_id"],
                    },
                    "geometry": mapping(ml),
                }
            )

    stats = {
        "polylines": len(polylines),
        "output_line_count": out_count,
        "out_of_network_count": sum(1 for i in issues if i["kind"] == "out_of_network"),
    }
    if issues:
        # 存在任何问题即不产出成果，由调用方保留上一次成功成果
        return RegistrationResult(ok=False, issues=issues, features=[], stats=stats)
    return RegistrationResult(ok=True, issues=[], features=out_features, stats=stats)


def shape_like(geojson_geom: dict) -> LineString | MultiLineString:
    """由 GeoJSON 几何构造 shapely 线几何（延迟导入避免循环依赖歧义）。"""
    from shapely.geometry import shape

    geom = shape(geojson_geom)
    if geom.geom_type not in ("LineString", "MultiLineString"):
        raise GeoValidationError("仅支持 LineString / MultiLineString 折线")
    return geom
