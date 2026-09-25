# 职业培训规划协作基础服务

本项目提供培训机构、教学场所、规划人员与培训资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在此之上，`training_planning` 包实现**新职业培训名额与就业反馈规划服务**：

- **岗位需求版本**：企业按地区与时间窗提交需求，同一业务编号形成版本链，区分意向、已验证、撤回状态；规划只采用每个系列版本号最高的已验证版本；
- **招生方案生成**：结合课程先修关系、教师工时、设备容量与历史结业就业率，一次生成需求优先、就业优先、均衡三套候选方案，并对过期需求、低就业率、工时/设备/先修不足给出缺口解释；
- **冻结与审批**：负责人冻结方案即固定输入快照，审批人审批通过才形成招生名额；同一期间最多一个冻结方案、并发审批最多一个生效方案；冻结或生效后的需求变化只登记影响提示，不回写方案；
- **学员事件与更正**：入学、转班、结业、就业反馈按业务编号幂等归并；对应统计期间已有发布报告的迟到反馈进入更正流程，已发布报告内容不改写，下一版报告应用挂起更正并保留版本链；
- **追溯接口**：每个名额可回溯需求来源版本、容量取舍记录与审批信息，每个统计期间可回溯报告修订与更正明细。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/training_planning/`：规划服务的表结构、方案生成算法、领域服务、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

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
PYTHONPATH=src python3 -m training_planning.acceptance
```

每条命令会在临时 SQLite 数据库中跑通各自业务的完整链路，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m training_planning.api --database planning.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者；规划服务在基础接口之外提供 `/planning/` 前缀的需求、课程、教师、设备、方案生成、冻结、审批、统计期间、学员事件、报告发布、名额追溯、影响提示与报告修订接口。服务重启后，SQLite 中的业务状态和审计链继续保留。

## 规划服务角色

在 `admin`、`operator`、`reviewer`、`auditor` 之外补充：`planner`（登记资源与需求、生成方案）、`director`（验证需求、冻结方案、发布报告）、`approver`（审批或驳回冻结方案）、`registrar`（登记学员事件）。
