# 板材直切排样 API（Panel Guillotine Nesting API）

面向小型加工坊的矩形零件母板排样服务。输入母板尺寸、锯缝宽度、母板数量上限和
**带数量与可旋转标记**的矩形零件，输出只由**贯穿当前余料区域的直切**
（guillotine cut）组成、可直接上机执行的切割方案：每件零件落在第几号母板、
坐标、朝向，以及按顺序编号的切割线和最终余料矩形。已保存的方案可一键导出为
确定性的机床 G-code（G0/G1 直切、板间 M0 换板）。

- 语言/框架：Python 3.12 · FastAPI · Pydantic v2
- 数据库：SQLite（单文件，WAL 模式，启动自动建库）
- 保证：零件不重叠、不越界；锯缝实际占位；同输入稳定（确定性算法 + 幂等存储）；
  排不下的零件以明确原因列出；G-code 导出前逐刀几何校验，异常方案整体拒绝；
  切割执行会话持久化，逐刀事件以乐观锁版本 + 幂等键在 SQLite 事务中校验，
  并发与重试不会破坏执行状态；设备离线期间完成的切刀可按批次回传，
  服务端在单事务中对账（一致前缀仅跳过、其余连续匹配后一次性推进）。

---

## 1. 一条命令启动（Docker Compose）

依赖安装在镜像构建时完成；容器启动时自动初始化 SQLite 数据库并运行服务。

```bash
docker compose up --build -d
```

这一条命令完成三件事：**安装依赖 → 初始化数据库 → 启动 API**。

- 访问地址：<http://localhost:8000>
- 健康检查：<http://localhost:8000/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>

> 端口在 `docker-compose.yml` 中映射为宿主机 `8000:8000`，可按需修改。
> 数据库保存在命名卷 `nesting-data`（容器内 `/app/data/nesting.db`），
> 重建容器数据不丢。

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止
docker compose down -v         # 停止并删除数据库卷
```

### 不用 Docker 的本地启动

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export NESTING_DB_PATH=./data/nesting.db   # 可选，默认即此路径
python -m app.init_db                       # 初始化数据库（或交给启动钩子自动完成）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

或直接执行容器同款入口脚本（初始化 + 启动一步到位）：

```bash
pip install -r requirements.txt
./entrypoint.sh
```

---

## 2. 接口

### `POST /api/v1/plans` — 创建/获取排样方案

相同请求是**幂等**的：`plan_id` 由请求内容的 SHA256 派生，重复提交返回同一份
已存方案（连创建时间都不变），因此“相同输入永远得到相同结果”。

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `board_width` | number, >0 | 母板宽（mm） |
| `board_height` | number, >0 | 母板高（mm） |
| `kerf_width` | number, ≥0，小于母板宽高 | 锯缝宽度（mm），默认 0 |
| `max_boards` | integer, ≥1 | 母板数量上限 |
| `parts` | array，≥1 | 零件清单 |
| `parts[].id` | string | 零件编号，同次请求内不可重复 |
| `parts[].width` / `height` | number, >0 | 零件宽 / 高（mm） |
| `parts[].quantity` | integer, ≥1 | 数量 |
| `parts[].rotatable` | boolean | 是否允许旋转 90° |

示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "board_width": 244,
    "board_height": 122,
    "kerf_width": 3,
    "max_boards": 3,
    "parts": [
      {"id": "door",  "width": 120, "height": 60, "quantity": 2, "rotatable": true},
      {"id": "shelf", "width": 60,  "height": 40, "quantity": 3, "rotatable": true}
    ]
  }'
```

响应关键字段：

```jsonc
{
  "plan_id": "a6df23c6-…",
  "status": "complete",            // complete=全部排入；partial=有未排零件
  "created_at": "2026-09-19T17:41:42Z",
  "summary": {
    "panels_used": 2,              // 实际使用母板数（≤ max_boards）
    "max_boards": 3,
    "placed_parts": 5,
    "unplaced_parts": 0,
    "utilization": 0.362806,       // 零件总面积 / 已用母板总面积
    "board_width": 244.0, "board_height": 122.0, "kerf_width": 3.0
  },
  "panels": [
    {
      "panel_no": 1,               // 母板编号，从 1 开始
      "width": 244.0, "height": 122.0,
      "placements": [              // 每件零件一条
        {"part_id": "door", "instance": 1, "rotated": false,
         "x": 0.0, "y": 0.0, "width": 120.0, "height": 60.0}
      ],
      "cuts": [                    // 按 step 顺序执行
        {
          "step": 1,
          "axis": "V",             // V=竖切(沿y贯穿当前区域全高)，H=横切(沿x贯穿全宽)
          "x": 120.0, "y": 0.0,
          "length": 122.0,         // 切割线长度（锯缝宽度方向不重复计长）
          "region": {"x": 0.0, "y": 0.0, "width": 244.0, "height": 122.0}
        }
      ],
      "remnants": [                // 最终余料矩形（锯缝不在余料内）
        {"x": 123.0, "y": 0.0, "width": 121.0, "height": 122.0}
      ]
    }
  ],
  "unplaced": []                   // 未排零件，见下
}
```

### 未排零件说明

排不下时 `status` 为 `"partial"`，`unplaced` 数组逐项列出：

```jsonc
"unplaced": [
  {"part_id": "A", "quantity": 1, "reason": "board_limit"},
  {"part_id": "B", "quantity": 4, "reason": "oversize"}
]
```

| reason | 含义 |
|---|---|
| `oversize` | 该零件无论是否旋转（受 `rotatable` 限制）都无法放入任何一块空母板（含锯缝占位后仍超限） |
| `board_limit` | 零件本身放得下，但现有母板的余料都排不进，且已达到 `max_boards` 上限 |

### `GET /api/v1/plans/{plan_id}` — 查询方案

返回与创建时完全一致的方案；不存在返回 `404`。

```bash
curl -s http://localhost:8000/api/v1/plans/a6df23c6-b019-79c7-2652-933b0d34d5e5
```

### `GET /api/v1/plans?limit=50&offset=0` — 方案列表

按创建时间倒序返回摘要（`plan_id / status / created_at / panels_used /
placed_parts / unplaced_parts`）及 `total`。

### `POST /api/v1/plans/{plan_id}/gcode` — 导出方案的 G-code

把**已保存**的方案按各母板 `cuts.step` 顺序翻译成确定性的 G0/G1 切割程序。
相同方案 + 相同参数永远得到逐字节相同的文本（不含时间戳等易变量）。

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `origin` | `top_left` / `top_right` / `bottom_left` / `bottom_right` | 机床原点所在的母板角 |
| `unit` | `mm` / `in` | 输出单位（`mm`→`G21`，`in`→`G20`） |
| `safe_z` | number, >0 | 安全抬刀高度（按 `unit` 计） |

示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/plans/a6df23c6-b019-79c7-2652-933b0d34d5e5/gcode \
  -H 'Content-Type: application/json' \
  -d '{"origin": "bottom_left", "unit": "mm", "safe_z": 5}'
```

响应：

```jsonc
{
  "plan_id": "a6df23c6-…",
  "origin": "bottom_left",
  "unit": "mm",
  "safe_z": 5.0,
  "line_count": 61,
  "total_cut_length": 883.0,      // 全部切割线长度之和（按 unit 计）
  "cut_map": [                    // 源 panel_no/step -> 该刀 G1 切割移动的行号（1 起）
    {"panel_no": 1, "step": 1, "line": 12},
    {"panel_no": 1, "step": 2, "line": 17}
  ],
  "gcode": "(PANEL-NESTING-API GCODE EXPORT)\n(PLAN a6df23c6-…)\n…M2\n"
}
```

生成的程序（`origin=bottom_left, unit=mm, safe_z=5` 时）形如：

```gcode
(PANEL-NESTING-API GCODE EXPORT)
(PLAN a6df23c6-…)
(ORIGIN bottom_left; UNIT mm; SAFE_Z 5.000)
G21
G90
G17
G0 Z5.000
(PANEL 1 OF 2: 244.000 X 122.000 mm)
(P1 S1 V)
G0 X120.000 Y122.000
G1 Z0.000
G1 X120.000 Y0.000
G0 Z5.000
…
M0 (CHANGE TO PANEL 2)
(PANEL 2 OF 2: 244.000 X 122.000 mm)
…
M2
```

**坐标变换**：排样坐标系以母板左上角为原点、x 向右、y 向下；机床坐标系以
`origin` 指定的母板角为原点，X/Y 轴取向保证整块母板落在第一象限（坐标非负）。
设板宽 W、板高 H，则 `(px, py)` 的映射为：

| origin | (mx, my) |
|---|---|
| `top_left` | `(px, py)` |
| `top_right` | `(W − px, py)` |
| `bottom_left` | `(px, H − py)` |
| `bottom_right` | `(W − px, H − py)` |

同一变换同时作用于每刀的**起点、终点与走刀方向**（先由 `axis/x/y/length`
得到线段两端点，再整体映射；镜像原点角会翻转对应轴的走刀方向）。`unit=in`
时全部坐标与 `total_cut_length` 按 ÷25.4 换算；`safe_z` 直接按 `unit` 解释。

**程序约定**：`G21/G20` 选单位、`G90` 绝对坐标、`G17` XY 平面；每刀一个块——
`G0` 快速定位到起点 → `G1 Z0` 下刀到切割平面（工件坐标 Z0）→ `G1` 切到终点 →
`G0` 抬刀到 `safe_z`；相邻母板之间插入 `M0` 暂停换板（仅板间，末尾不加）；
程序以 `M2` 结束。注释 `(P<母板号> S<step> V|H)` 与 `cut_map` 一一对应。

**导出前校验**：导出前逐刀校验——切割线（含两端点）仍在母板内、沿切割方向
贯穿其 `region`、`region` 在母板内、`step` 从 1 连续编号。任何违规都**整体
拒绝**并返回 `409`，不输出任何部分 G-code：

```jsonc
{
  "detail": {
    "error": "invalid_plan_geometry",
    "message": "方案切割线未通过导出前几何校验，未生成任何 G-code",
    "violations": [
      {"panel_no": 1, "step": 1, "code": "cut_outside_board",
       "message": "切割线 (150.0, 0.0)->(150.0, 100.0) 超出母板 100.0 x 100.0"}
    ]
  }
}
```

`violations[].code` 取值：`cut_outside_board` / `cut_not_spanning_region` /
`region_outside_board` / `non_sequential_steps` / `invalid_axis` /
`invalid_length` / `invalid_region` / `invalid_cut`。

### `POST /api/v1/plans/{plan_id}/sessions` — 启动切割执行会话

从**已保存**的方案启动一个持久化的切割执行会话（`201`；方案不存在 `404`）。
会话把方案的全部切割线按 `(panel_no, step)` 展平为全局执行序列（与 G-code
导出同序），由客户端逐刀汇报推进。同一方案可启动多个会话（例如多次下料），
各自独立推进、互不影响。

**无切割线的方案**（零件恰好铺满母板、或全部零件未排入）启动即完成：
`status="completed"`、`completed=true`、`completed_at` 即刻写入。

响应（`201`，与查询/事件接口同构的 `SessionResponse`）：

```jsonc
{
  "session_id": "3f8c…",
  "plan_id": "a6df…",
  "status": "active",            // active / completed
  "completed": false,            // 全部刀是否已完成
  "version": 0,                  // 乐观锁版本，每完成一刀 +1
  "completed_cuts": 0,           // 已完成刀数
  "total_cuts": 20,              // 总刀数（启动时快照）
  "current_panel": 1,            // 当前母板（下一刀所在板）；已完成为 null
  "next_cut": {                  // 下一刀；已完成为 null
    "panel_no": 1, "step": 1, "axis": "V",
    "x": 120.0, "y": 0.0, "length": 122.0,
    "region": {"x": 0.0, "y": 0.0, "width": 244.0, "height": 122.0}
  },
  "created_at": "2026-09-19T17:41:42Z",
  "updated_at": "2026-09-19T17:41:42Z",
  "completed_at": null
}
```

### `GET /api/v1/sessions/{session_id}` — 查询切割会话

返回当前母板、下一刀、已完成数与 `completed` 状态（结构同上）；
不存在返回 `404`。

```bash
curl -s http://localhost:8000/api/v1/sessions/3f8c…
```

### `POST /api/v1/sessions/{session_id}/events` — 提交“完成当前刀”事件

按 `panel_no/step` 顺序逐刀汇报“当前刀已切完”。请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `expected_version` | int, ≥0 | 客户端看到的会话版本（乐观锁） |
| `idempotency_key` | string, 1~128 字符 | 幂等键（会话内唯一） |
| `panel_no` | int, ≥1 | 刚完成的刀所在母板编号 |
| `step` | int, ≥1 | 刚完成的刀在该母板上的序号 |

```bash
curl -s -X POST http://localhost:8000/api/v1/sessions/3f8c…/events \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "idempotency_key": "p1-s1", "panel_no": 1, "step": 1}'
```

服务端在**单个 SQLite 写事务（`BEGIN IMMEDIATE`）**内依次执行：查幂等记录 →
校验会话未完成 → 校验 `expected_version` 等于当前版本 → 校验所报
`(panel_no, step)` 正是当前刀 → 推进版本与进度并记录事件，然后一次性提交。
由此保证：

- **幂等重试**：同一 `idempotency_key` + 相同请求体重试，返回首次应用的
  原始响应（HTTP `200`，逐字段一致），状态不重复推进；
- **同键不同请求**：`409 idempotency_key_reuse`；
- **并发/旧版本**：`expected_version` 与当前版本不一致 →
  `409 version_conflict`，状态不推进。`BEGIN IMMEDIATE` 使并发写者在库级
  串行，同版本并发请求只有一个能应用，其余读到推进后的版本并被拒绝；
- **跳步/乱序**：所报刀不是当前刀 → `409 cut_mismatch`，状态不推进；
- **已完成**：`409 session_completed`；会话不存在 `404`。

成功返回推进后的会话状态（结构同 `SessionResponse`）。冲突响应示例：

```jsonc
{
  "detail": {
    "error": "version_conflict",
    "message": "expected_version=3 与当前版本 5 不一致，状态未推进",
    "expected_version": 3,
    "current_version": 5
  }
}
```

`detail.error` 取值：`version_conflict` / `cut_mismatch` /
`session_completed` / `idempotency_key_reuse`。

### `POST /api/v1/sessions/{session_id}/event-batches` — 离线回传切刀批次

设备离线期间完成的刀，恢复联网后按**执行顺序**整批回传。请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `batch_id` | string, 1~128 字符 | 批次幂等键（会话内唯一） |
| `base_version` | int, ≥0 | 设备离线前看到的会话版本；批次第一刀对应该版本之后的第一刀 |
| `cuts` | array（可为空） | 按执行顺序排列的切刀列表，每项 `{panel_no, step}` |

```bash
curl -s -X POST http://localhost:8000/api/v1/sessions/3f8c…/event-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "offline-2026-09-20-001",
    "base_version": 2,
    "cuts": [
      {"panel_no": 1, "step": 3},
      {"panel_no": 1, "step": 4},
      {"panel_no": 1, "step": 5}
    ]
  }'
```

服务端在**单个 SQLite 写事务（`BEGIN IMMEDIATE`）**内依次执行：查批次幂等
记录 → 校验 `base_version` 不超过当前版本 → 把 `base_version` 之后服务端
**已完成的区间与批次前缀逐项对账**（一致前缀仅跳过；首个不一致整批拒绝）
→ 其余后缀必须**连续匹配当前方案**的下一刀序列 → 验证通过后一次性推进
`version` 与 `completed_cuts` 并记录批次，然后一次性提交。由此保证：

- **对账跳过**：离线期间服务端已通过单刀事件（或先前批次）推进的区间，
  与批次前缀一致的部分只跳过不重复推进；
- **整批原子**：前缀对账或后缀匹配任一失败 → `409 batch_mismatch`，
  整批不推进（`detail` 含 `phase`=`reconcile`/`apply`、批次内下标 `index`
  及 `expected`/`received`）；`base_version` 比服务端还新 →
  `409 version_conflict`；
- **幂等重放**：同一 `batch_id` + 相同请求体重试，返回首次应用的原始响应
  （HTTP `200`，逐字段一致），状态不重复推进；同键不同请求 →
  `409 batch_id_reuse`；
- **并发安全**：批次与单刀、批次与批次在库级串行，后到者基于最新状态重新
  对账——已被覆盖的批次自然转为“全部跳过”，不会重复推进。

成功返回 `skipped`/`applied` 数量及最新会话状态：

```jsonc
{
  "skipped": 1,                  // 对账一致而跳过的刀数
  "applied": 2,                  // 本批次实际推进的刀数
  "session": {                   // 推进后的最新会话状态（SessionResponse）
    "session_id": "3f8c…",
    "status": "active",
    "version": 5,
    "completed_cuts": 5,
    "total_cuts": 20,
    "current_panel": 1,
    "next_cut": {"panel_no": 1, "step": 6, "axis": "H", "…": "…"},
    "created_at": "2026-09-20T08:00:00Z",
    "updated_at": "2026-09-20T09:30:00Z",
    "completed_at": null
  }
}
```

`detail.error` 取值：`batch_mismatch` / `version_conflict` /
`batch_id_reuse`；会话不存在返回 `404`。

### `GET /health`

返回 `{"status":"ok"}`，供容器健康检查与负载均衡探活。

---

## 3. 切割方案的几何语义

坐标系：母板左上角为原点 `(0,0)`，**x 向右、y 向下**，单位 mm。

- 零件 `placements[].x/y` 为其左上角；`rotated=true` 表示相对原始宽高旋转了 90°，
  此时返回的 `width/height` 是旋转后实际占位（`width` 仍对应 x 方向）。
- 每件零件放置在一个矩形余料区域的左下角。放置后做 1~2 刀：
  1. 第一刀沿“富余较长”的方向**贯穿当前区域**（`sx ≥ sy` 先竖切，否则先横切，
     平局先竖切）；
  2. 第二刀（若需要）在零件所在一侧贯穿该侧，把零件与余料分开。
- 每块母板的 `cuts[]` 已按 `step` 排好可执行顺序：**先执行的刀会把后续刀所在的
  区域完整切出来**，即每一刀都贯穿它 `region` 字段所指的当前余料矩形。
  - `axis:"V"`：竖刀在 `x` 处，沿 y 方向，长度 = `region.height`；
  - `axis:"H"`：横刀在 `y` 处，沿 x 方向，长度 = `region.width`；
  - 锯缝在切刀线的“右侧/下侧”占位，宽 `kerf_width`；相邻零件/余料坐标之间
    会自动空出这一条缝。
- `remnants[]` 是所有切割完成后剩下的可用矩形（不含锯缝、不含零件）。

排样引擎在生成方案时内置严格自检：逐刀模拟贯穿切、校验切割后矩形集合恰为
“零件 ∪ 余料”、面积平衡（零件 + 余料 + 锯缝 = 母板面积）、零件不重叠不越界、
数量守恒。任何自检失败都会直接报错而不会返回错误方案。

### 确定性

- 零件实例按（最长边降序、面积降序、id、输入序号）固定排序；
- 所有候选位置用固定元组打分：优先用已有母板，其次浪费面积最小，再以
  原始朝向优先、母板号、x、y 裁决平局；
- 因此无论零件在请求中的先后顺序如何，同一输入（含数量、旋转标记）总是得到
  同一份方案，且结果以内容哈希持久化、天然幂等。

---

## 4. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（方案/会话/事件/G-code/健康检查）+ 启动建库
│   ├── models.py        # 请求/响应 Pydantic 模型与参数校验
│   ├── nesting.py       # 直切排样核心算法 + 几何自检 + 响应装配
│   ├── gcode.py         # G-code 导出：坐标变换、导出前校验、确定性文本生成
│   ├── services.py      # 业务编排、幂等落库、切割会话推进（事件/批次事务校验）
│   ├── database.py      # SQLite 连接、建表、plan_id 摘要、方案/会话 CRUD、写事务
│   └── init_db.py       # python -m app.init_db 建库入口
├── tests/               # unittest：核心算法/随机不变量/持久化幂等/G-code/会话与批次并发/API 端到端
├── entrypoint.sh        # 容器启动：建库 + uvicorn（一条命令）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 5. 运行测试

```bash
pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
```

测试覆盖：恰好放满、锯缝切分、旋转启用/禁用、母板上限、超大件、编号连续、
零锯缝铺满、**300 组随机算例的贯穿切模拟与面积/重叠/数量不变量**、
结果稳定性（含输入乱序）、SQLite 幂等存取、**G-code 坐标变换/确定性/行号映射/
总长/导出前校验**、**切割会话的启动/查询/逐刀推进/零切割即完成、幂等重试原样
返回、同键冲突、旧版本与跳步拒绝、多线程并发竞争（同版本唯一胜者、同键全员
重放、带重试跑完整个会话）**、**离线批次的对账跳过/整批原子拒绝/空批次同步/
完成后重传、批次幂等重放与同键冲突、重叠批次并发合成、批次与单刀混合并发
跑完整个会话**，以及基于 TestClient 的 HTTP 端到端。
