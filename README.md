# 新职业培训名额与就业反馈规划服务

本项目面向培训机构的新职业招生规划场景，把企业岗位需求、课程与教学容量、招生方案审批和学员结业就业反馈连成一条连续数据链，避免仅凭热门名称扩招。服务保存企业按地区和时间窗提交的岗位需求版本（意向、已验证、撤回），结合课程先修关系、教师工时、设备容量与历史结业反馈一次生成多套招生方案并给出缺口解释；负责人选择方案即冻结输入快照，审批通过才形成招生名额，之后的需求变化只产生影响提示。学员入学、转班、结业与就业反馈按业务编号幂等归并，迟到反馈进入对应统计期间的更正流程而不改写已发布报告；同一职业、地区、期间最多存在一个生效方案，管理接口可追溯每个名额的需求来源、容量取舍与报告修订。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、规划与就业反馈服务、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由、并发审批和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance
PYTHONPATH=src python3 -m skills_workspace.planning_acceptance
```

第一条命令验收基础登记链路；第二条在临时 SQLite 数据库中执行完整规划链路：需求版本与状态流转、多策略方案生成、快照冻结、影响提示、审批产生名额、学员事件幂等归并、报告发布与迟到更正，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## 核心业务规则

- **需求版本**：每次提交追加新版本，状态按 意向 → 已验证 → 撤回 流转；规划时只取最新版本，自动排除已撤回、已过有效期和时间窗不重叠的需求并记录排除原因。
- **方案生成**：按保守、稳健、积极三种策略生成多套方案。名额取 需求折算目标、教师工时（工时 ÷ 课时 × 班容）、设备工位（同类型设备按先修顺序分配）与先修课程结业人数 的最小值，历史就业率按策略折算目标，缺口解释写明约束来源与数值。
- **冻结与审批**：负责人选择方案即冻结输入快照；审批人与选择人不得相同，审批通过才生成招生名额；同一职业、地区、统计期间由数据库唯一索引保证最多一个生效方案，并发审批只有一个成功。冻结后需求变化只产生 `impact_notices`，不回改方案。
- **学员事件**：入学、转班、结业、就业反馈以业务编号幂等归并，相同内容重发返回原结果，不同内容复用编号返回冲突；名额占用随入学、转班、结业实时变化且不超过审批名额。
- **报告与更正**：统计期间报告发布后，迟到事件进入该期间的待处理更正，原报告不被改写；再次发布只吸收待处理更正并生成新的修订版本，旧版本保留可查。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，主要接口：

- 需求：`POST /demands`、`POST /demands/verify`、`POST /demands/withdraw`、`GET /demands`；
- 课程与容量：`POST /courses`、`POST /teacher-capacities`、`POST /equipment-capacities`、`GET /courses`；
- 方案：`POST /plans/generate`、`POST /plans/select`、`POST /plans/reject`、`POST /plans/approve`、`GET /plans`、`GET /plan?plan_id=`；
- 名额与提示：`GET /quotas`、`GET /impact-notices?plan_id=`；
- 学员事件：`POST /student-events`、`GET /student?student_id=`；
- 报告：`POST /reports/publish`、`GET /reports`、`GET /report-corrections?period=`；
- 追溯：`GET /trace/quota?quota_id=`，返回名额的需求来源、容量取舍、影响提示与报告修订；
- 基础能力：`POST /organizations`、`POST /actors`、`POST /sites`、`POST /domain-records`、`GET /audit-events`。

服务重启后，SQLite 中的业务状态和审计链继续保留。
