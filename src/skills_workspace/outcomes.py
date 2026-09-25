"""学员事件幂等归并、统计报告发布与迟到反馈更正流程。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, digest
from .errors import ConflictError, NotFoundError, ValidationError
from .models import WriteReceipt
from .planning import PlanningService
from .stats import period_outcomes, serialize_rate

EVENT_TYPES = ("enroll", "transfer", "graduate", "employment")


class OutcomeService(PlanningService):
    """在规划服务之上提供学员事件归并与统计报告能力。"""

    # ------------------------------------------------------------------
    # 学员事件
    # ------------------------------------------------------------------

    def _ensure_student(self, connection, student_id: str, display_name: str) -> None:
        row = connection.execute("SELECT 1 FROM students WHERE student_id=?",
                                 (student_id,)).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO students(student_id,display_name,created_at) VALUES(?,?,?)",
                (student_id, display_name, self._now()),
            )

    def _load_quota(self, connection, quota_id: str):
        row = connection.execute("SELECT * FROM quotas WHERE quota_id=?", (quota_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"招生名额 {quota_id} 不存在")
        return row

    def _active_enrollment(self, connection, student_id: str, quota_id: str | None = None):
        if quota_id is None:
            return connection.execute(
                "SELECT * FROM enrollments WHERE student_id=? AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1", (student_id,),
            ).fetchone()
        return connection.execute(
            "SELECT * FROM enrollments WHERE student_id=? AND quota_id=? AND status='active'",
            (student_id, quota_id),
        ).fetchone()

    def _apply_enroll(self, connection, *, event_id: str, student_id: str,
                      period: str, payload: dict[str, Any]) -> tuple[str, str]:
        quota_id = str(payload.get("quota_id", "")).strip()
        if not quota_id:
            raise ValidationError("入学事件必须携带 quota_id")
        quota = self._load_quota(connection, quota_id)
        if quota["period"] != period:
            raise ValidationError("入学事件期间与名额所属期间不一致")
        if self._active_enrollment(connection, student_id) is not None:
            raise ConflictError("学员存在未结束的在读记录，不能重复入学")
        if quota["enrolled"] >= quota["headcount"]:
            raise ConflictError("该名额已招满")
        connection.execute(
            "INSERT INTO enrollments(student_id,quota_id,status,enrolled_event_id,updated_at) "
            "VALUES(?,?,'active',?,?)",
            (student_id, quota_id, event_id, self._now()),
        )
        connection.execute("UPDATE quotas SET enrolled=enrolled+1 WHERE quota_id=?", (quota_id,))
        return quota["course_id"], quota_id

    def _apply_transfer(self, connection, *, event_id: str, student_id: str,
                        payload: dict[str, Any]) -> tuple[str, str]:
        from_quota_id = str(payload.get("from_quota_id", "")).strip()
        to_quota_id = str(payload.get("to_quota_id", "")).strip()
        if not from_quota_id or not to_quota_id:
            raise ValidationError("转班事件必须携带 from_quota_id 与 to_quota_id")
        if from_quota_id == to_quota_id:
            raise ValidationError("转班前后名额不能相同")
        enrollment = self._active_enrollment(connection, student_id, from_quota_id)
        if enrollment is None:
            raise ValidationError("学员在原来源名额下没有在读记录")
        to_quota = self._load_quota(connection, to_quota_id)
        if to_quota["enrolled"] >= to_quota["headcount"]:
            raise ConflictError("目标名额已招满")
        connection.execute(
            "UPDATE enrollments SET status='transferred_out', updated_at=? "
            "WHERE student_id=? AND quota_id=?",
            (self._now(), student_id, from_quota_id),
        )
        connection.execute("UPDATE quotas SET enrolled=enrolled-1 WHERE quota_id=?", (from_quota_id,))
        connection.execute(
            "INSERT INTO enrollments(student_id,quota_id,status,enrolled_event_id,updated_at) "
            "VALUES(?,?,'active',?,?) "
            "ON CONFLICT(student_id,quota_id) DO UPDATE SET status='active', "
            "enrolled_event_id=excluded.enrolled_event_id, updated_at=excluded.updated_at",
            (student_id, to_quota_id, event_id, self._now()),
        )
        connection.execute("UPDATE quotas SET enrolled=enrolled+1 WHERE quota_id=?", (to_quota_id,))
        return to_quota["course_id"], to_quota_id

    def _apply_graduate(self, connection, *, student_id: str,
                        payload: dict[str, Any]) -> tuple[str, str]:
        quota_id = str(payload.get("quota_id", "")).strip()
        if not quota_id:
            raise ValidationError("结业事件必须携带 quota_id")
        quota = self._load_quota(connection, quota_id)
        enrollment = self._active_enrollment(connection, student_id, quota_id)
        if enrollment is None:
            raise ValidationError("学员在该名额下没有在读记录")
        connection.execute(
            "UPDATE enrollments SET status='graduated', updated_at=? "
            "WHERE student_id=? AND quota_id=?",
            (self._now(), student_id, quota_id),
        )
        connection.execute("UPDATE quotas SET enrolled=enrolled-1 WHERE quota_id=?", (quota_id,))
        return quota["course_id"], quota_id

    def _apply_employment(self, connection, *, student_id: str,
                          payload: dict[str, Any]) -> tuple[str, str | None]:
        employed = payload.get("employed")
        if not isinstance(employed, bool):
            raise ValidationError("就业反馈必须携带布尔值 employed")
        row = connection.execute(
            "SELECT e.quota_id, q.course_id FROM enrollments e "
            "JOIN quotas q ON q.quota_id=e.quota_id "
            "WHERE e.student_id=? AND e.status='graduated' "
            "ORDER BY e.updated_at DESC LIMIT 1",
            (student_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("学员尚未结业，不能登记就业反馈")
        return row["course_id"], row["quota_id"]

    def _record_correction_if_late(self, connection, *, event_id: str, student_id: str,
                                   event_type: str, period: str, course_id: str | None) -> bool:
        """已发布报告的统计期间收到迟到事件时，登记待处理更正而不改写报告。"""

        latest = connection.execute(
            "SELECT * FROM reports WHERE period=? ORDER BY version DESC LIMIT 1", (period,)
        ).fetchone()
        if latest is None or latest["status"] != "published":
            return False
        correction_id = uuid.uuid4().hex
        detail = {"event_id": event_id, "student_id": student_id, "event_type": event_type,
                  "course_id": course_id, "report_id": latest["report_id"],
                  "report_version": latest["version"]}
        connection.execute(
            "INSERT INTO report_corrections(correction_id,period,event_id,status,detail_json,created_at) "
            "VALUES(?,?,?,'pending',?,?)",
            (correction_id, period, event_id,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), self._now()),
        )
        append_event(connection, actor_id="system", action="report.correction_pending",
                     resource_type="report", resource_id=latest["report_id"],
                     detail={"correction_id": correction_id, "event_id": event_id,
                             "event_type": event_type},
                     occurred_at=self._now())
        return True

    def record_student_event(self, *, request_id: str, actor_id: str, event_id: str,
                             student_id: str, event_type: str, period: str,
                             payload: dict[str, Any],
                             student_name: str | None = None) -> WriteReceipt:
        """按业务编号幂等归并入学、转班、结业与就业反馈事件。"""

        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        receipt_payload = {"actor_id": actor_id, "event_id": event_id, "student_id": student_id,
                           "event_type": event_type, "period": period, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "registrar")
            event_id = self._identifier(event_id, "event_id")
            student_id = self._identifier(student_id, "student_id")
            if event_type not in EVENT_TYPES:
                raise ValidationError("event_type 必须是 enroll/transfer/graduate/employment 之一")
            period = self._period(period)
            display_name = self._text(student_name or student_id, "student_name")
            event_hash = digest({"student_id": student_id, "event_type": event_type,
                                 "period": period, "payload": payload})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM student_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != event_hash:
                        raise ConflictError("业务编号已登记过不同内容的事件")
                    return "student_event", event_id, {"event_id": event_id, "merged": True,
                                                       "correction": False}
                self._ensure_student(connection, student_id, display_name)
                if event_type == "enroll":
                    course_id, quota_id = self._apply_enroll(
                        connection, event_id=event_id, student_id=student_id,
                        period=period, payload=payload)
                elif event_type == "transfer":
                    course_id, quota_id = self._apply_transfer(
                        connection, event_id=event_id, student_id=student_id, payload=payload)
                elif event_type == "graduate":
                    course_id, quota_id = self._apply_graduate(
                        connection, student_id=student_id, payload=payload)
                else:
                    course_id, quota_id = self._apply_employment(
                        connection, student_id=student_id, payload=payload)
                connection.execute(
                    "INSERT INTO student_events(event_id,student_id,event_type,period,course_id,"
                    "quota_id,payload_json,payload_hash,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id, student_id, event_type, period, course_id, quota_id,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True), event_hash,
                     actor_id, self._now()),
                )
                correction = self._record_correction_if_late(
                    connection, event_id=event_id, student_id=student_id,
                    event_type=event_type, period=period, course_id=course_id)
                append_event(connection, actor_id=actor_id, action="student_event.recorded",
                             resource_type="student_event", resource_id=event_id,
                             detail={"student_id": student_id, "event_type": event_type,
                                     "period": period, "course_id": course_id,
                                     "correction": correction},
                             occurred_at=self._now())
                return "student_event", event_id, {"event_id": event_id, "merged": False,
                                                   "correction": correction}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_student_event",
                                    payload=receipt_payload, create=create)

    # ------------------------------------------------------------------
    # 统计报告与更正
    # ------------------------------------------------------------------

    def publish_report(self, *, request_id: str, actor_id: str, period: str) -> WriteReceipt:
        """发布统计期间报告；再发布只吸收待处理更正并生成新修订版本。"""

        payload = {"actor_id": actor_id, "period": period}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            period = self._period(period)

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT * FROM reports WHERE period=? ORDER BY version DESC LIMIT 1", (period,)
                ).fetchone()
                pending = connection.execute(
                    "SELECT * FROM report_corrections WHERE period=? AND status='pending' "
                    "ORDER BY created_at", (period,)
                ).fetchall()
                if latest is not None and latest["status"] == "published" and not pending:
                    raise ConflictError("报告已发布且没有待处理的更正")
                version = 1 if latest is None else int(latest["version"]) + 1
                if latest is not None:
                    connection.execute("UPDATE reports SET status='superseded' WHERE report_id=?",
                                       (latest["report_id"],))
                stats = period_outcomes(connection, period)
                courses = []
                for course_id in sorted(stats):
                    entry = stats[course_id]
                    rate = (None if entry["graduated"] == 0
                            else entry["employed"] / entry["graduated"])
                    courses.append({"course_id": course_id, **entry,
                                    "employment_rate": serialize_rate(rate)})
                totals = {
                    "enrolled": sum(item["enrolled"] for item in courses),
                    "transferred_in": sum(item["transferred_in"] for item in courses),
                    "graduated": sum(item["graduated"] for item in courses),
                    "employed": sum(item["employed"] for item in courses),
                }
                report_id = uuid.uuid4().hex
                summary = {"period": period, "version": version, "courses": courses,
                           "totals": totals, "corrections_applied": len(pending),
                           "generated_at": self._now()}
                connection.execute(
                    "INSERT INTO reports(report_id,period,version,status,summary_json,published_by,"
                    "published_at) VALUES(?,?,?,'published',?,?,?)",
                    (report_id, period, version,
                     json.dumps(summary, ensure_ascii=False, sort_keys=True),
                     actor_id, self._now()),
                )
                for correction in pending:
                    connection.execute(
                        "UPDATE report_corrections SET status='applied', applied_report_id=? "
                        "WHERE correction_id=?",
                        (report_id, correction["correction_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="report.published",
                             resource_type="report", resource_id=report_id,
                             detail={"period": period, "version": version,
                                     "corrections_applied": len(pending),
                                     "supersedes": None if latest is None else latest["report_id"]},
                             occurred_at=self._now())
                return "report", report_id, {"report_id": report_id, "period": period,
                                             "version": version,
                                             "corrections_applied": len(pending)}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_report", payload=payload, create=create)

    def list_reports(self, period: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        query = "SELECT * FROM reports"
        if period:
            query += " WHERE period=?"
            parameters.append(period)
        query += " ORDER BY period, version"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{**dict(row), "summary": json.loads(row["summary_json"])} for row in rows]

    def list_corrections(self, period: str, status: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = [period]
        query = "SELECT * FROM report_corrections WHERE period=?"
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY created_at, correction_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def get_student(self, student_id: str) -> dict[str, Any]:
        connection = self.database.connection
        student = connection.execute("SELECT * FROM students WHERE student_id=?",
                                     (student_id,)).fetchone()
        if student is None:
            raise NotFoundError("学员不存在")
        enrollments = connection.execute(
            "SELECT * FROM enrollments WHERE student_id=? ORDER BY updated_at", (student_id,)
        ).fetchall()
        events = connection.execute(
            "SELECT event_id,event_type,period,course_id,quota_id,recorded_at FROM student_events "
            "WHERE student_id=? ORDER BY recorded_at, event_id", (student_id,)
        ).fetchall()
        return {**dict(student), "enrollments": [dict(row) for row in enrollments],
                "events": [dict(row) for row in events]}
