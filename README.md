# 食品过敏原配方合规 API（Food Allergen Recipe Compliance API）

基于 **Python + FastAPI + PostgreSQL** 的过敏原合规评估服务：

- 登记**不可变原料**及其过敏原标记（含量 + 交叉接触）
- 登记**法域阈值规则**（草稿 → 发布，发布后不可变）
- 按法域建立**可追溯规则集**：带 `base_version` 的草稿提交（阈值 ppm 归一化）、
  重叠冲突检测、发布为不可变 `rule_set_version`（含摘要与内容指纹）
- 提交**带版本的嵌套配方**（配方可引用子配方，版本发布后不可变）
- **评估**：按用量递归展开子配方，完成质量/浓度单位换算与交叉接触累积，
  检测循环引用、未知原料与超阈值，输出**按稳定顺序排列、含触发路径**的
  标签义务（obligations）、警告（warnings）与违规（violations）
- 评估请求**绑定配方版本**，并可显式绑定已发布的**规则集版本快照**，
  通过幂等键支持**幂等重放**；快照保证同一规则可随时复现历史结论
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

启动后可直接运行端到端示例脚本（覆盖：登记 → 规则/规则集发布 → 评估 →
快照绑定重放 → 幂等重放 → 最小缺失集合）：

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

### 3.3 按法域建立可追溯规则集（草稿 → 发布快照）

规则集（rule set）把某法域的一组阈值规则作为**整体草稿**管理：每次提交把阈值
**统一归一化为 ppm**；通过 `base_version` 做乐观并发控制；发布时检测重叠冲突，
成功后生成不可变的 `rule_set_version` 及摘要（规则数、过敏原、action 分布、
ppm 阈值范围、规则码列表、内容指纹 `digest`）。

```bash
# 1) 创建空草稿（current_version = 0）
curl -X POST http://localhost:8000/rule-sets -H 'Content-Type: application/json' \
  -d '{"code":"eu-2026","jurisdiction":"EU","name":"EU Allergens 2026"}'

# 2) 提交草稿（base_version 初始为 0；单位可为 ppm/ppb/mg-kg/g-kg/%，服务端归一化）
curl -X PUT http://localhost:8000/rule-sets/eu-2026 \
  -H 'Content-Type: application/json' \
  -d '{
        "base_version": 0,
        "rules": [
          {"allergen":"gluten","threshold":0,"unit":"ppm",
           "action":"declare","applies_to":"contains"},
          {"code":"gluten-prohibit-20ppm","allergen":"gluten","threshold":20000,"unit":"ppb",
           "action":"prohibit","applies_to":"contains"},
          {"allergen":"peanut","threshold":0,"unit":"ppm",
           "action":"warning","applies_to":"cross_contact"}
        ]
      }'
```

- 提交成功后 `current_version` 自增为 `1`，响应中的每条规则都带 `threshold_ppm`
  （`20000 ppb`、`0.002%` 都会归一化为 `20.0 ppm`；规则缺省 `code` 时自动生成
  稳定的 `auto-001` 形式编码）。
- **并发**：`base_version` 与当前版本不符时返回 `409 VERSION_CONFLICT`，
  响应体含 `current_version`，且**不写入任何内容**（服务端用原子 compare-and-swap
  兜底并发提交）。

**重叠与冲突规则**（发布时校验）：同一过敏原、同一 `applies_to` 且 ppm 阈值相等
（含边界，比较在 ppm 归一化后进行）的规则视为**重叠**。

| 重叠的 action 组合 | 是否可并存 |
| ---- | ---- |
| `declare` + `warning` | ✅ 可并存（同阈值分别产生义务与警告） |
| `prohibit` + 任意 action（含另一条 `prohibit`） | ❌ 冲突 |
| 两条相同 action（如两条 `declare`）同阈值 | ❌ 冲突 |

有冲突时发布被拒绝，返回 `409 RULE_SET_CONFLICT` 与**稳定排序**的冲突明细
（按 `allergen, applies_to, threshold_ppm, rule_a, rule_b` 排序）：

```json
{
  "detail": {
    "code": "RULE_SET_CONFLICT",
    "message": "rule set 'eu-2026' has 1 conflicting rule(s); publishing is rejected",
    "conflicts": [
      {"allergen": "gluten", "applies_to": "contains", "threshold_ppm": 20.0,
       "rule_a": {"code": "gluten-declare", "action": "declare"},
       "rule_b": {"code": "gluten-prohibit-20ppm", "action": "prohibit"},
       "reason": "prohibit_conflict"}
    ]
  }
}
```

```bash
# 3) 发布：冻结为不可变版本（版本号 = 发布时的 current_version）
curl -X POST http://localhost:8000/rule-sets/eu-2026/publish

# 对同一草稿重复发布是幂等的（返回相同 version/digest）；
# 再次 PUT 提交（base_version 为上一版）后发布会生成新的 rule_set_version
curl http://localhost:8000/rule-sets/eu-2026/versions
curl http://localhost:8000/rule-sets/eu-2026/versions/1
```

空草稿发布返回 `422 RULE_SET_EMPTY`。已发布快照**永久不可变**，新的提交/发布
不会影响旧版本。

### 3.4 提交带版本的嵌套配方

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

### 3.5 评估（绑定版本 + 幂等；可绑定规则集快照）

```bash
# 旧行为：不绑定规则集时，使用该法域所有“已发布的单条规则”（完全向后兼容）
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cake-eu-v1-20260923' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU"}'

# 新行为：显式绑定不可变规则集快照进行评估
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cake-eu-rs1-20260929' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU",
       "rule_set_code":"eu-2026","rule_set_version":1}'
```

首次返回 `201 Created`；**相同幂等键 + 相同请求体重放**返回 `200 OK` 且
`"replayed": true`、`evaluation_id` 不变；**相同键 + 不同请求体**返回
`409 IDEMPOTENCY_CONFLICT`。幂等键也可放在请求体 `idempotency_key` 字段。

绑定规则集时：

- `rule_set_code` 与 `rule_set_version` **必须同时提供**（否则 `422`）；
  `jurisdiction` 必须与规则集法域一致（不一致 → `422 RULE_SET_JURISDICTION_MISMATCH`）。
- 绑定的版本不存在 → `404 RULE_SET_VERSION_NOT_FOUND`；存在但**尚未发布**（仍是
  草稿版本）→ `409 RULE_SET_NOT_PUBLISHED`（均含 `current_version`）；规则集
  不存在 → `404 RULE_SET_NOT_FOUND`。这些失败不占用幂等键。
- 评估只应用该快照内的规则（忽略法域下的单条规则），结果中带
  `"rule_set": {"code", "version", "summary"}`。快照不可变，因此**任何时候按
  evaluation ID 或规则集版本查询，都重放同一规则、得到相同结论**。

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
  "rule_set": {"code": "eu-2026", "version": 1, "summary": {"rule_count": 3, "digest": "…", ...}},
  "evaluation_id": "…",
  "replayed": false
}
```

> `rule_set` 块仅在绑定规则集的评估中出现；旧版（不绑定）响应结构保持不变。

所有列表均**稳定排序**：义务/警告/违规按 `(allergen, rule_code)`，触发路径按
路径字符串排序——同一输入永远得到逐字节一致的结果。

**按评估 ID 或规则集版本查询（固定重放）**：

```bash
# 按评估 ID：返回固定的 summary、配方版本与完整结果
curl http://localhost:8000/evaluations/{evaluation_id}

# 按规则集版本：列出绑定该快照的全部评估，每项都含 summary、配方版本与结果
curl http://localhost:8000/rule-sets/eu-2026/versions/1/evaluations

# 等价过滤也可在通用列表上使用
curl 'http://localhost:8000/evaluations?rule_set_code=eu-2026&rule_set_version=1'
```

### 3.6 输入不完整 → 最小缺失集合

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

### 3.7 循环检测

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
| POST | `/rule-sets` | 创建按法域的空规则集草稿（`current_version=0`） |
| PUT  | `/rule-sets/{code}` | 提交草稿（ppm 归一化；`base_version` 乐观锁，不符 → 409 含 `current_version`） |
| POST | `/rule-sets/{code}/publish` | 发布不可变 `rule_set_version`（冲突 → 409 明细；幂等） |
| GET  | `/rule-sets`、`/rule-sets/{code}` | 查询规则集与当前草稿 |
| GET  | `/rule-sets/{code}/versions`、`/rule-sets/{code}/versions/{v}` | 查询已发布版本快照与摘要 |
| GET  | `/rule-sets/{code}/versions/{v}/evaluations` | 按快照版本固定重放其评估（含摘要、配方版本、结果） |
| POST | `/recipes` | 创建配方 |
| POST | `/recipes/{code}/versions` | 新增草稿版本（版本号唯一） |
| PUT/DELETE | `/recipes/{code}/versions/{v}` | 仅草稿可改/删 |
| POST | `/recipes/{code}/versions/{v}/publish` | 发布版本（幂等，发布后不可变） |
| GET  | `/recipes…` | 查询配方及版本 |
| POST | `/evaluations` | 评估（需 `Idempotency-Key` 头或 `idempotency_key` 字段；可选 `rule_set_code`+`rule_set_version` 绑定快照） |
| GET  | `/evaluations`、`/evaluations/{id}` | 查询评估结果（列表支持 `rule_set_code`、`rule_set_version` 过滤） |
| GET  | `/units`、`/health`、`/` | 元信息 |

## 5. 评估语义

1. **递归展开**：从绑定的配方版本出发，按 `用量 / 子配方产出` 缩放每一片叶子
   原料的质量；所有质量统一换算为克。
2. **过敏原累积**：`过敏原质量 = Σ(叶子质量 × 含量)`；最终浓度
   `= 过敏原总质量 / 批次产出`，以 ppm 表示。
3. **交叉接触累积**：叶子原料的 `cross_contact` 标记沿路径向上累积为警告。
4. **规则匹配**：对每个适用规则，`contains` 类在浓度 `≥ 阈值`（边界含等号）时
   触发（阈值 0 表示“存在即触发”），`cross_contact` 类在存在交叉接触路径时触发；
   按 `action` 分别归入 obligations / warnings / violations。规则来源有两种：
   未绑定规则集时取法域下所有已发布的单条规则（旧行为）；绑定规则集时只应用
   指定的**不可变快照**，因此结论随快照永久可复现。
5. **未监管过敏原**：配方中存在但（本次评估的规则来源）无任何规则覆盖的过敏原，
   列入 `unregulated_allergens`，不阻断评估。

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

- **不可变性**：原料登记即冻结；规则与配方版本经 `publish` 后冻结（改/删 → 409）；
  规则集发布生成不可变 `rule_set_version`（含规则快照与摘要/digest），保证任意
  历史评估可解释、可复现。
- **可追溯规则集**：草稿按法域维护，提交时阈值统一归一化为 ppm；`base_version`
  乐观并发（初始 0，成功自增，不符 → 409 带 `current_version`，原子 CAS 不写入）。
  发布前检测重叠：同过敏原 + 同 applies_to + 同 ppm 阈值（含边界）的规则，
  `declare`/`warning` 可并存，`prohibit` 与任一 action 冲突、同 action 重复亦冲突；
  冲突明细稳定排序并拒绝发布。
- **版本绑定**：评估请求必须指定 `recipe_version`；子配方可钉版本或解析为
  “最新已发布”，解析结果随结果持久化（`resolved_recipes`）。评估还可显式绑定
  `rule_set_code`+`rule_set_version`；缺失/未发布返回稳定错误码，按评估 ID 或
  规则集版本查询固定返回摘要、配方版本与结果，并重放同一规则快照。
- **向后兼容**：不带规则集绑定的评估与全部旧接口行为保持不变。
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
  models.py               # Ingredient / Rule / RuleSet / RuleSetVersion /
                          # Recipe / RecipeVersion / Evaluation
  schemas.py              # 请求校验（单位、编码规范化、>100% 拦截、乐观锁/绑定）
  units.py                # 质量(g)与浓度(ppm)换算
  services/
    expansion.py          # 递归展开、循环检测、最小缺失集合
    evaluation.py         # 过敏原累积、规则匹配（支持规则集快照）、稳定排序输出
    rule_sets.py          # ppm 归一化、重叠冲突检测、摘要/digest、绑定解析
  routers/                # ingredients / rules / rule_sets / recipes / evaluations
tests/                    # pytest（SQLite 内存库，60+ 个用例）
examples/quickstart.sh    # 端到端 curl 示例
Dockerfile / docker-compose.yml
```
