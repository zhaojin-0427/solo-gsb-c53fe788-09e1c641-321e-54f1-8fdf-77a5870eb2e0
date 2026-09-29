# 食品过敏原配方合规 API（Food Allergen Recipe Compliance API）

基于 **Python + FastAPI + PostgreSQL** 的过敏原合规评估服务：

- 登记**不可变原料**及其过敏原标记（含量 + 交叉接触）
- 登记**法域阈值规则**（草稿 → 发布，发布后不可变）
- 提交**带版本的嵌套配方**（配方可引用子配方，版本发布后不可变）
- **评估**：按用量递归展开子配方，完成质量/浓度单位换算与交叉接触累积，
  检测循环引用、未知原料与超阈值，输出**按稳定顺序排列、含触发路径**的
  标签义务（obligations）、警告（warnings）与违规（violations）
- 评估请求**绑定配方版本**，通过幂等键支持**幂等重放**
- 输入不完整时返回**最小缺失集合**（minimal missing set）

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动两个服务：

| 服务 | 说明 | 端口 |
| ---- | ---- | ---- |
| `db`  | PostgreSQL 16（数据持久化到 `pgdata` volume） | 5432 |
| `api` | FastAPI + Uvicorn（等待 db 健康检查后启动，自动建表） | 8000 |

## 2. 访问

| 地址 | 说明 |
| ---- | ---- |
| <http://localhost:8000/docs> | Swagger UI 交互式文档 |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/units> | 支持的质量/浓度单位 |

## 3. 快速示例

启动后可直接运行端到端示例脚本（覆盖：登记 → 发布 → 评估 → 幂等重放 → 最小缺失集合）：

```bash
bash examples/quickstart.sh
```

或按下面的 curl 步骤手动执行。

### 3.1 登记不可变原料

```bash
curl -X POST http://localhost:8000/ingredients \
  -H 'Content-Type: application/json' \
  -d '{
        "code": "almond_flour",
        "name": "Almond Flour",
        "allergens": [{"allergen": "tree_nuts", "concentration": 100, "unit": "%"}],
        "cross_contact": ["peanut"]
      }'
```

- `allergens[].unit` 支持浓度单位：`ppm`、`ppb`、`mg/kg`、`g/kg`、`%`
- `cross_contact` 为“可能含有”的交叉接触过敏原清单
- 原料**一经登记不可修改**；重复登记同一 `code` 返回 `409 INGREDIENT_EXISTS`

### 3.2 登记并发布法域阈值规则

```bash
curl -X POST http://localhost:8000/rules \
  -H 'Content-Type: application/json' \
  -d '{
        "code": "eu-gluten-prohibit-20ppm",
        "jurisdiction": "EU",
        "allergen": "gluten",
        "threshold": 20,
        "unit": "ppm",
        "action": "prohibit",
        "applies_to": "contains"
      }'

curl -X POST http://localhost:8000/rules/eu-gluten-prohibit-20ppm/publish
```

- `action`：`declare`（标签义务）/ `warning`（警告）/ `prohibit`（超阈值违规）
- `applies_to`：`contains`（配方带入）/ `cross_contact`（交叉接触）
- 规则**发布后不可变**（`PUT/DELETE` 返回 `409 RULE_IMMUTABLE`）；评估只使用已发布规则

### 3.3 提交带版本的嵌套配方

```bash
# 子配方 filling v1：100 g 杏仁粉 + 100 g 糖
curl -X POST http://localhost:8000/recipes -H 'Content-Type: application/json' \
  -d '{"code":"filling","name":"Almond Filling"}'
curl -X POST http://localhost:8000/recipes/filling/versions -H 'Content-Type: application/json' \
  -d '{
        "version": 1,
        "items": [
          {"kind":"ingredient","code":"almond_flour","quantity":100,"unit":"g"},
          {"kind":"ingredient","code":"sugar","quantity":100,"unit":"g"}
        ]
      }'
curl -X POST http://localhost:8000/recipes/filling/versions/1/publish

# 蛋糕 cake v1：400 g 子配方 filling + 1 kg 小麦粉
curl -X POST http://localhost:8000/recipes -H 'Content-Type: application/json' \
  -d '{"code":"cake","name":"Almond Cake"}'
curl -X POST http://localhost:8000/recipes/cake/versions -H 'Content-Type: application/json' \
  -d '{
        "version": 1,
        "items": [
          {"kind":"recipe","code":"filling","quantity":400,"unit":"g"},
          {"kind":"ingredient","code":"wheat_flour","quantity":1,"unit":"kg"}
        ]
      }'
curl -X POST http://localhost:8000/recipes/cake/versions/1/publish
```

- 条目 `kind` 为 `ingredient`（叶子原料）或 `recipe`（子配方）
- 子配方可**钉版本**（`"version": 2`）或省略版本号（评估时解析为**最新已发布版本**，解析结果记录在 `resolved_recipes` 中）
- 质量单位支持：`mg`、`g`、`kg`、`oz`、`lb`
- 可选 `yield_quantity`/`yield_unit` 指定批次产出（如烘焙失水）；缺省为各原料用量之和
- 版本**发布后不可变**；同一版本号不可重复创建

### 3.4 评估（绑定版本 + 幂等）

```bash
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cake-eu-v1-20260923' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU"}'
```

首次返回 `201 Created`；**相同幂等键 + 相同请求体重放**返回 `200 OK` 且
`"replayed": true`、`evaluation_id` 不变；**相同键 + 不同请求体**返回
`409 IDEMPOTENCY_CONFLICT`。幂等键也可放在请求体 `idempotency_key` 字段。

响应节选（数值为上述示例的真实计算结果）：

```json
{
  "recipe": {"code": "cake", "version": 1},
  "jurisdiction": "EU",
  "status": "completed",
  "total_mass_g": 1400.0,
  "resolved_recipes": [{"code": "cake", "version": 1}, {"code": "filling", "version": 1}],
  "allergens": [
    {"allergen": "gluten",    "concentration_ppm": 71428.571429,  "concentration_percent": 7.142857},
    {"allergen": "tree_nuts", "concentration_ppm": 142857.142857, "concentration_percent": 14.285714}
  ],
  "obligations": [
    {
      "allergen": "tree_nuts",
      "rule_code": "eu-tree-nuts-declare",
      "action": "declare",
      "source": "contains",
      "threshold_ppm": 0.0,
      "concentration_ppm": 142857.142857,
      "trigger_paths": [
        {"path": ["cake@v1", "filling@v1", "almond_flour"],
         "ingredient": "almond_flour", "ingredient_mass_g": 200.0,
         "allergen_mass_g": 200.0, "contribution_ppm": 142857.142857}
      ]
    }
  ],
  "warnings": [
    {"allergen": "peanut", "rule_code": "eu-peanut-may-contain", "action": "warning",
     "source": "cross_contact",
     "trigger_paths": [{"path": ["cake@v1", "filling@v1", "almond_flour"], ...}]}
  ],
  "violations": [
    {"allergen": "gluten", "rule_code": "eu-gluten-prohibit-20ppm", "action": "prohibit",
     "threshold_ppm": 20.0, "concentration_ppm": 71428.571429, ...}
  ],
  "unregulated_allergens": [],
  "evaluation_id": "…",
  "replayed": false
}
```

所有列表均**稳定排序**：义务/警告/违规按 `(allergen, rule_code)`，触发路径按
路径字符串排序——同一输入永远得到逐字节一致的结果。

### 3.5 输入不完整 → 最小缺失集合

```bash
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: broken-1' \
  -d '{"recipe_code":"broken","recipe_version":1,"jurisdiction":"EU"}'
```

```json
{
  "detail": {
    "code": "INCOMPLETE_INPUT",
    "message": "evaluation input is incomplete; supply the minimal missing set",
    "missing": [
      {"type": "ingredient", "code": "mystery", "reason": "unknown_ingredient"},
      {"type": "recipe", "code": "nonexistent", "version": 7, "reason": "unknown_recipe"}
    ]
  }
}
```

缺失集合**去重且按稳定顺序排列**，只包含直接缺失的引用（未知原料、未知配方、
未知/未发布版本），补齐后重发同一请求即可。

### 3.6 循环检测

配方图存在环（如 `a → b → a`）时返回 `422`：

```json
{"detail": {"code": "RECIPE_CYCLE", "cycle": ["a@v1", "b@v1", "a@v1"], "message": "..."}}
```

## 4. API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/ingredients` | 登记不可变原料（重复 code → 409） |
| GET  | `/ingredients`、`/ingredients/{code}` | 查询 |
| POST | `/rules` | 登记规则（草稿） |
| PUT/DELETE | `/rules/{code}` | 仅草稿可改/删（已发布 → 409） |
| POST | `/rules/{code}/publish` | 发布规则（幂等，发布后不可变） |
| GET  | `/rules`、`/rules/{code}` | 查询（支持 `jurisdiction`、`status` 过滤） |
| POST | `/recipes` | 创建配方 |
| POST | `/recipes/{code}/versions` | 新增草稿版本（版本号唯一） |
| PUT/DELETE | `/recipes/{code}/versions/{v}` | 仅草稿可改/删 |
| POST | `/recipes/{code}/versions/{v}/publish` | 发布版本（幂等，发布后不可变） |
| GET  | `/recipes…` | 查询配方及版本 |
| POST | `/evaluations` | 评估（需 `Idempotency-Key` 头或 `idempotency_key` 字段） |
| GET  | `/evaluations`、`/evaluations/{id}` | 查询评估结果 |
| GET  | `/units`、`/health`、`/` | 元信息 |

## 5. 评估语义

1. **递归展开**：从绑定的配方版本出发，按 `用量 / 子配方产出` 缩放每一片叶子
   原料的质量；所有质量统一换算为克。
2. **过敏原累积**：`过敏原质量 = Σ(叶子质量 × 含量)`；最终浓度
   `= 过敏原总质量 / 批次产出`，以 ppm 表示。
3. **交叉接触累积**：叶子原料的 `cross_contact` 标记沿路径向上累积为警告。
4. **规则匹配**：对每个已发布规则，`contains` 类在浓度 `≥ 阈值` 时触发
   （阈值 0 表示“存在即触发”），`cross_contact` 类在存在交叉接触路径时触发；
   按 `action` 分别归入 obligations / warnings / violations。
5. **未监管过敏原**：配方中存在但法域无任何已发布规则覆盖的过敏原，列入
   `unregulated_allergens`，不阻断评估。

## 6. 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 运行测试（使用 SQLite 内存库，无需 PostgreSQL）
pytest

# 本地起服务（默认连 localhost:5432 的 PostgreSQL，可用 DATABASE_URL 覆盖）
uvicorn app.main:app --reload
```

## 7. 设计要点

- **不可变性**：原料登记即冻结；规则与配方版本经 `publish` 后冻结（改/删 → 409），
  保证任意历史评估可解释、可复现。
- **版本绑定**：评估请求必须指定 `recipe_version`；子配方可钉版本或解析为
  “最新已发布”，解析结果随结果持久化（`resolved_recipes`）。
- **幂等重放**：评估结果按幂等键持久化；重放返回同一 `evaluation_id` 与
  `replayed: true`；同键不同请求体 → 409。数据库唯一约束兜底并发冲突。
- **最小缺失集合**：展开过程收集全部直接缺失引用（去重、稳定排序）后一次性
  返回 `422 INCOMPLETE_INPUT`；失败评估不占用幂等键。
- **稳定输出**：所有列表确定性排序，数值统一 `round(., 6)`，便于审计与 diff。

## 8. 目录结构

```
app/
  main.py                 # FastAPI 入口、建表、统一错误格式
  config.py / db.py       # DATABASE_URL、引擎与会话
  models.py               # Ingredient / Rule / Recipe / RecipeVersion / Evaluation
  schemas.py              # 请求校验（单位、编码规范化、>100% 拦截）
  units.py                # 质量(g)与浓度(ppm)换算
  services/
    expansion.py          # 递归展开、循环检测、最小缺失集合
    evaluation.py         # 过敏原累积、规则匹配、稳定排序输出
  routers/                # ingredients / rules / recipes / evaluations
tests/                    # pytest（SQLite 内存库，40 个用例）
examples/quickstart.sh    # 端到端 curl 示例
Dockerfile / docker-compose.yml
```
