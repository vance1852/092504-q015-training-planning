"""规划服务的 SQLite 表结构,在基础服务表之上追加。"""

from __future__ import annotations

from pathlib import Path

from skills_workspace.storage import Database


PLANNING_SCHEMA = """
PRAGMA foreign_keys = ON;
-- 岗位需求系列:企业按业务编号提交,同一编号的内容演进形成版本。
CREATE TABLE IF NOT EXISTS demand_series (
    series_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    demand_key TEXT NOT NULL,
    enterprise_name TEXT NOT NULL,
    region TEXT NOT NULL,
    occupation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, demand_key)
);
-- 岗位需求版本:内容不可变,状态在意向/已验证/撤回间迁移。
CREATE TABLE IF NOT EXISTS demand_versions (
    version_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES demand_series(series_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    headcount INTEGER NOT NULL CHECK(headcount > 0),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('intent','verified','withdrawn')),
    note TEXT NOT NULL DEFAULT '',
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL,
    decided_by TEXT REFERENCES actors(actor_id),
    decided_at TEXT,
    decision_reason TEXT,
    UNIQUE(series_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_demand_versions_series ON demand_versions(series_id, version_no);
-- 课程与先修关系;先修课程必须先登记,图中不会成环。
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    occupation TEXT NOT NULL,
    duration_hours INTEGER NOT NULL CHECK(duration_hours > 0),
    class_size INTEGER NOT NULL CHECK(class_size > 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS course_prerequisites (
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    prerequisite_id TEXT NOT NULL REFERENCES courses(course_id),
    PRIMARY KEY (course_id, prerequisite_id),
    CHECK (course_id <> prerequisite_id)
);
CREATE TABLE IF NOT EXISTS teachers (
    teacher_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    available_hours INTEGER NOT NULL CHECK(available_hours >= 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teacher_courses (
    teacher_id TEXT NOT NULL REFERENCES teachers(teacher_id),
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    PRIMARY KEY (teacher_id, course_id)
);
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units >= 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS course_equipment (
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
    units_per_class INTEGER NOT NULL CHECK(units_per_class > 0),
    PRIMARY KEY (course_id, equipment_id)
);
-- 招生方案:同一期间可生成多轮,每轮三套策略;冻结时快照已经随方案保存。
CREATE TABLE IF NOT EXISTS enrollment_plans (
    plan_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    period_key TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    strategy TEXT NOT NULL,
    generation_no INTEGER NOT NULL CHECK(generation_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','frozen','approved','rejected','stale')),
    input_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    frozen_by TEXT REFERENCES actors(actor_id),
    frozen_at TEXT,
    decided_by TEXT REFERENCES actors(actor_id),
    decided_at TEXT,
    decision_reason TEXT,
    UNIQUE(organization_id, period_key, strategy, generation_no)
);
CREATE TABLE IF NOT EXISTS plan_allocations (
    plan_id TEXT NOT NULL REFERENCES enrollment_plans(plan_id),
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    classes INTEGER NOT NULL CHECK(classes >= 0),
    quota INTEGER NOT NULL CHECK(quota >= 0),
    raw_demand INTEGER NOT NULL,
    adjusted_demand INTEGER NOT NULL,
    PRIMARY KEY (plan_id, course_id)
);
-- 名额的需求来源:indirect=1 表示该需求经由先修支撑关系间接贡献。
CREATE TABLE IF NOT EXISTS plan_allocation_demands (
    plan_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES demand_versions(version_id),
    indirect INTEGER NOT NULL CHECK(indirect IN (0, 1)),
    headcount INTEGER NOT NULL,
    PRIMARY KEY (plan_id, course_id, version_id, indirect),
    FOREIGN KEY (plan_id, course_id) REFERENCES plan_allocations(plan_id, course_id)
);
-- 缺口解释:course_id 为 NULL 表示计划级缺口(如需求有效期不匹配)。
CREATE TABLE IF NOT EXISTS plan_gaps (
    gap_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES enrollment_plans(plan_id),
    course_id TEXT,
    gap_type TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_gaps_plan ON plan_gaps(plan_id);
-- 审批通过后形成的招生名额。
CREATE TABLE IF NOT EXISTS enrollment_quotas (
    quota_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    period_key TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES enrollment_plans(plan_id),
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    classes INTEGER NOT NULL CHECK(classes >= 0),
    quota INTEGER NOT NULL CHECK(quota > 0),
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    created_at TEXT NOT NULL
);
-- 冻结或生效之后的需求变化只形成影响提示,不回写方案。
CREATE TABLE IF NOT EXISTS impact_notices (
    notice_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES enrollment_plans(plan_id),
    series_id TEXT NOT NULL REFERENCES demand_series(series_id),
    version_id TEXT NOT NULL REFERENCES demand_versions(version_id),
    change_type TEXT NOT NULL,
    impact TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_notices_plan ON impact_notices(plan_id);
-- 统计期间:同一组织内区间不得重叠,保证事件归属唯一。
CREATE TABLE IF NOT EXISTS stat_periods (
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    period_key TEXT NOT NULL,
    start_on TEXT NOT NULL,
    end_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, period_key)
);
-- 学员事件:按业务编号幂等归并,is_late 标记对应期间已有发布报告的迟到反馈。
CREATE TABLE IF NOT EXISTS student_events (
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('enrolled','transferred','graduated','employed')),
    student_id TEXT NOT NULL,
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    occurred_on TEXT NOT NULL,
    period_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    is_late INTEGER NOT NULL CHECK(is_late IN (0, 1)),
    recorded_by TEXT NOT NULL REFERENCES actors(actor_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_student_events_period ON student_events(organization_id, period_key);
-- 已发布报告内容不可改写,新版本应用挂起的更正。
CREATE TABLE IF NOT EXISTS period_reports (
    report_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    period_key TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('published','superseded')),
    content_json TEXT NOT NULL,
    corrections_applied INTEGER NOT NULL,
    published_by TEXT NOT NULL REFERENCES actors(actor_id),
    published_at TEXT NOT NULL,
    UNIQUE(organization_id, period_key, version_no)
);
CREATE TABLE IF NOT EXISTS report_corrections (
    correction_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    period_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','applied')),
    report_id TEXT REFERENCES period_reports(report_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY (organization_id, event_key) REFERENCES student_events(organization_id, event_key)
);
"""


class PlanningDatabase(Database):
    """在基础服务表结构之上追加规划服务表结构的数据库。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(PLANNING_SCHEMA)
