"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_demands (
    demand_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    enterprise_id TEXT NOT NULL,
    occupation TEXT NOT NULL,
    region TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    headcount INTEGER NOT NULL CHECK(headcount >= 1),
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('intent', 'verified', 'withdrawn')),
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (demand_id, version)
);
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    occupation TEXT NOT NULL,
    name TEXT NOT NULL,
    duration_hours INTEGER NOT NULL CHECK(duration_hours > 0),
    equipment_type TEXT NOT NULL,
    max_class_size INTEGER NOT NULL CHECK(max_class_size > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS course_prerequisites (
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    prerequisite_id TEXT NOT NULL REFERENCES courses(course_id),
    PRIMARY KEY (course_id, prerequisite_id),
    CHECK(course_id <> prerequisite_id)
);
CREATE TABLE IF NOT EXISTS teacher_capacities (
    teacher_id TEXT NOT NULL,
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    period TEXT NOT NULL,
    available_hours INTEGER NOT NULL CHECK(available_hours >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (teacher_id, course_id, period)
);
CREATE TABLE IF NOT EXISTS equipment_capacities (
    equipment_type TEXT NOT NULL,
    period TEXT NOT NULL,
    seats INTEGER NOT NULL CHECK(seats >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (equipment_type, period)
);
CREATE TABLE IF NOT EXISTS plan_batches (
    batch_id TEXT PRIMARY KEY,
    occupation TEXT NOT NULL,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES plan_batches(batch_id),
    occupation TEXT NOT NULL,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('conservative', 'balanced', 'aggressive')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'selected', 'approved', 'rejected')),
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    selected_by TEXT,
    selected_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS plans_one_effective
    ON plans(occupation, region, period) WHERE status = 'approved';
CREATE TABLE IF NOT EXISTS plan_lines (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    course_id TEXT NOT NULL,
    quota INTEGER NOT NULL CHECK(quota >= 0),
    demand_headcount INTEGER NOT NULL,
    limiting_factor TEXT NOT NULL,
    gap_explanation TEXT NOT NULL,
    PRIMARY KEY (plan_id, course_id)
);
CREATE TABLE IF NOT EXISTS quotas (
    quota_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    course_id TEXT NOT NULL,
    period TEXT NOT NULL,
    headcount INTEGER NOT NULL CHECK(headcount >= 0),
    enrolled INTEGER NOT NULL DEFAULT 0 CHECK(enrolled >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, course_id)
);
CREATE TABLE IF NOT EXISTS impact_notices (
    notice_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    demand_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollments (
    student_id TEXT NOT NULL REFERENCES students(student_id),
    quota_id TEXT NOT NULL REFERENCES quotas(quota_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'transferred_out', 'graduated')),
    enrolled_event_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (student_id, quota_id)
);
CREATE TABLE IF NOT EXISTS student_events (
    event_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('enroll', 'transfer', 'graduate', 'employment')),
    period TEXT NOT NULL,
    course_id TEXT,
    quota_id TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('published', 'superseded')),
    summary_json TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(period, version)
);
CREATE TABLE IF NOT EXISTS report_corrections (
    correction_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES student_events(event_id),
    status TEXT NOT NULL CHECK(status IN ('pending', 'applied')),
    applied_report_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._write_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；单连接上的事务通过锁串行化。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
