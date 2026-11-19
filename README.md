# 地籍变更系统（分割 / 合并）

FastAPI + SQLite + Shapely 实现的网页应用：导入单一平面坐标系下、整数坐标、
无洞、互不重叠的 GeoJSON 宗地，支持宗地分割/合并变更的预核验、问题区域预览、
原子确认提交，以及现势与历史边界、变更流水查询。

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

## 样例数据（`samples/`）

- `initial.geojson`：P1、P2 两宗共边相邻宗地，用于初始导入；
- `split_p1.geojson`：P1 拆分为 P1A/P1B（共边 x=5）；
- `merge_p1.geojson`：P1A/P1B 合并回 P1C；
- `invalid_change.geojson`：只覆盖 P1 左部的问题面，用于预览缺口。

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -v
```

覆盖：导入校验（非整数/带洞/重叠/重复导入）、缺口与侵占预核验、预核验不落库、
非法提交被拒且数据不变、拆分、合并、历史与版本快照、多线程竞争提交（200+409）、
失败回滚。
