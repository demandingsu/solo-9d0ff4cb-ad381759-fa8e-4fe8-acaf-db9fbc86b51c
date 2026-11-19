# 地籍变更系统（分割 / 合并 / 图幅配准）

FastAPI + SQLite + Shapely 实现的网页应用：导入单一平面坐标系下、整数坐标、
无洞、互不重叠的 GeoJSON 宗地，支持宗地分割/合并变更的预核验、问题区域预览、
原子确认提交，以及现势与历史边界、变更流水查询；并支持将旧图幅上描绘的像素
界址线，经四控制点双三角分片仿射配准叠合到选定版本的地籍底图。

## 启动（Docker Compose）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> 即可操作。SQLite 数据文件持久化在 `./data/cadastre.db`。

本地无 Docker 时：

```bash
pip install -r requirements.txt
CADASTRE_DB=./data/cadastre.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 业务规则

1. **导入**：FeatureCollection，每个 Feature 的 `properties.parcel_id` 为宗地号；
   几何必须是 Polygon、整数坐标、无内环（无洞）、面积大于 0；初始宗地之间不得
   面积重叠，共边允许。仅允许在空库导入一次。
2. **变更请求**：指定 `retired_ids`（退出的现势宗地号）与 `new_parcels`（接替新面）。
   - 新面之间不得面积重叠（共边允许）；
   - 新面不得侵占任何未退出的现势宗地；
   - 新面并集须**恰好**等于退出旧面并集：少了即「缺口」，多了即「越界」；
   - 新宗地号不得与现势宗地号冲突。
3. **两阶段操作**：先 `POST /api/changes/plan` 预核验，返回缺口/侵占/越界/互叠的
   GeoJSON 几何，网页在 SVG 地图上以红/橙/紫/粉色叠加预览；核验通过后才能
   `POST /api/changes/commit` 确认。
4. **乐观并发**：每个请求携带 `base_version`。提交在 `BEGIN IMMEDIATE` 事务内复核
   当前版本，版本不一致返回 409。基于同一版本的两笔提交只有一笔生效。
5. **失败原子性**：任何核验或写入失败都回滚整个事务，现势与历史保持原状。
6. **历史保留**：宗地版本行 append-only——退出时追加 `retired` 行（旧行不改），
   新设时追加 `current` 行；可按宗地号查全版本边界，也可按版本号查当时的现势快照。

## 图幅配准（旧图幅界址线叠合）

档案员在网页「图幅配准」页选择一版地籍（按版本号），上传像素折线
（LineString / MultiLineString 的 FeatureCollection），并按**周边顺序**录入
四个像素控制点 G1..G4 及各自对应的**登记顶点**（必须是所选版本某宗地环上的顶点）。

### 配准模型

- 沿第 1、3 点的连线（分界线）把控制网分成两个三角区：
  A = (G1, G2, G3)、B = (G1, G4, G3)，各用三对点求解一片二维仿射变换；
- 像素折线先与控制网（两三角区并集）求交后按区分段，每段用所在三角区的
  仿射变换转到登记坐标，分界线两侧结果在交点处连续；
- 落在分界线上的线段被两片各产出一次，经 union 去重后**只输出一次**；
  同源折线的分段经 linemerge 重新接成连续折线。

### 问题定位（任一成立即本次计算失败，不产出新成果）

| 问题 kind | 含义 |
| --- | --- |
| `degenerate_triangle` | 源图或底图的三角区退化（三点共线/重合，面积为 0），区分 `space=pixel/ground` |
| `triangle_overlap` | 源图或底图两个三角区内部面积重叠（控制点未按周边顺序等） |
| `out_of_network` | 折线线段超出控制网，返回问题截段的像素几何与长度 |
| `gcp_not_vertex` | 登记控制点不在所选地籍版本的宗地顶点集合内 |

更正控制点后对同一作业**重算整批**：成功则整体替换成果；失败则保留上一次
成功成果，下载接口始终返回该份成果；从未成功过的作业下载返回 409。
网页叠图预览与 GeoJSON 下载读取的是数据库中同一份成果。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本与全部现势宗地 |
| POST | `/api/import` | 初始导入 GeoJSON |
| POST | `/api/changes/plan` | 预核验（不落库），返回问题区域几何 |
| POST | `/api/changes/commit` | 确认提交（原子写入，版本冲突返回 409） |
| GET | `/api/history/parcel/{id}` | 单宗地全历史版本边界 |
| GET | `/api/history/version/{v}` | 截至 v 版本的现势快照 |
| GET | `/api/txns` | 变更流水列表 |
| GET | `/api/txns/{id}` | 单笔流水详情（退出/新设明细 + 核验报告） |
| POST | `/api/registration/jobs` | 新建配准作业并执行首次整批计算 |
| GET | `/api/registration/jobs` | 配准作业列表（含是否保有成果） |
| GET | `/api/registration/jobs/{id}` | 作业详情：控制点、折线、问题与成果 |
| POST | `/api/registration/jobs/{id}/recompute` | 更正后重算整批（失败保留上次成果） |
| GET | `/api/registration/jobs/{id}/result.geojson` | 下载最近一次成功成果（无成果返回 409） |

配准请求体示例：

```json
{
  "base_version": 1,
  "gcps": [
    {"pixel": [20, 20],   "ground": [0, 0]},
    {"pixel": [220, 20],  "ground": [10, 0]},
    {"pixel": [220, 220], "ground": [10, 10]},
    {"pixel": [20, 220],  "ground": [0, 10]}
  ],
  "polylines": {"type": "FeatureCollection", "features": [/* LineString 要素 */]}
}
```

## 样例数据（`samples/`）

- `initial.geojson`：P1、P2 两宗共边相邻宗地，用于初始导入；
- `split_p1.geojson`：P1 拆分为 P1A/P1B（共边 x=5）；
- `merge_p1.geojson`：P1A/P1B 合并回 P1C；
- `invalid_change.geojson`：只覆盖 P1 左部的问题面，用于预览缺口；
- `registration_request.json` / `registration_polylines.geojson`：图幅配准示例
  （控制点与折线分两个文件，请求文件中的 `polylines_file` 仅作引用说明）；
- `registration_out_of_network.json`：折线超出控制网的失败示例，验证问题定位
  与「失败后仍可下载上次成功成果」。

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -v
```

覆盖：导入校验（非整数/带洞/重叠/重复导入）、缺口与侵占预核验、预核验不落库、
非法提交被拒且数据不变、拆分、合并、历史与版本快照、多线程竞争提交（200+409）、
失败回滚；以及图幅配准的双三角仿射求解、跨区分段连续性、分界线去重、
退化/内部重叠/超网/非顶点四类问题定位、更正重算、失败后下载上次成果、
预览与下载同源一致性（共 38 项）。
