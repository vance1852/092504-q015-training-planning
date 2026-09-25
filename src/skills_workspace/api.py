"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .outcomes import OutcomeService
from .service import DomainService
from .storage import Database


def _query(parsed, name: str, default: str | None = None) -> str | None:
    """从查询串里取单个参数。"""

    values = parse_qs(parsed.query).get(name)
    return values[0] if values else default


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = _query(parsed, "site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [item.__dict__ for item in
                                   service.list_domain_data(site_id, _query(parsed, "category"))]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(_query(parsed, "after_sequence", "0") or "0")
            return 200, {"items": service.audit_events(after)}
        planning = _planning_route(service, method, parsed, body, actor_id)
        if planning is not None:
            return planning
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _planning_route(service: DomainService, method: str, parsed, body: dict[str, Any],
                    actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """分派新职业培训名额与就业反馈规划服务的接口。"""

    planner = service if isinstance(service, OutcomeService) else None
    if planner is None:
        return None
    path = parsed.path
    if method == "POST" and path == "/demands":
        return _receipt(planner.submit_demand(actor_id=actor_id, **body))
    if method == "POST" and path == "/demands/verify":
        return _receipt(planner.verify_demand(actor_id=actor_id, **body))
    if method == "POST" and path == "/demands/withdraw":
        return _receipt(planner.withdraw_demand(actor_id=actor_id, **body))
    if method == "GET" and path == "/demands":
        include_history = _query(parsed, "include_history", "") == "true"
        return 200, {"items": planner.list_demands(_query(parsed, "occupation"),
                                                   _query(parsed, "region"), include_history)}
    if method == "POST" and path == "/courses":
        return _receipt(planner.register_course(actor_id=actor_id, **body))
    if method == "GET" and path == "/courses":
        return 200, {"items": planner.list_courses(_query(parsed, "occupation"))}
    if method == "POST" and path == "/teacher-capacities":
        return _receipt(planner.set_teacher_capacity(actor_id=actor_id, **body))
    if method == "POST" and path == "/equipment-capacities":
        return _receipt(planner.set_equipment_capacity(actor_id=actor_id, **body))
    if method == "POST" and path == "/plans/generate":
        return _receipt(planner.generate_plans(actor_id=actor_id, **body))
    if method == "POST" and path == "/plans/select":
        return _receipt(planner.select_plan(actor_id=actor_id, **body))
    if method == "POST" and path == "/plans/reject":
        return _receipt(planner.reject_plan(actor_id=actor_id, **body))
    if method == "POST" and path == "/plans/approve":
        return _receipt(planner.approve_plan(actor_id=actor_id, **body))
    if method == "GET" and path == "/plans":
        return 200, {"items": planner.list_plans(batch_id=_query(parsed, "batch_id"),
                                                 occupation=_query(parsed, "occupation"),
                                                 region=_query(parsed, "region"),
                                                 period=_query(parsed, "period"))}
    if method == "GET" and path == "/plan":
        plan_id = _query(parsed, "plan_id", "")
        if not plan_id:
            raise ValidationError("plan_id 不能为空")
        return 200, planner.get_plan(plan_id)
    if method == "GET" and path == "/quotas":
        return 200, {"items": planner.list_quotas(plan_id=_query(parsed, "plan_id"),
                                                  period=_query(parsed, "period"))}
    if method == "GET" and path == "/impact-notices":
        plan_id = _query(parsed, "plan_id", "")
        if not plan_id:
            raise ValidationError("plan_id 不能为空")
        return 200, {"items": planner.list_impact_notices(plan_id)}
    if method == "POST" and path == "/student-events":
        return _receipt(planner.record_student_event(actor_id=actor_id, **body))
    if method == "GET" and path == "/student":
        student_id = _query(parsed, "student_id", "")
        if not student_id:
            raise ValidationError("student_id 不能为空")
        return 200, planner.get_student(student_id)
    if method == "POST" and path == "/reports/publish":
        return _receipt(planner.publish_report(actor_id=actor_id, **body))
    if method == "GET" and path == "/reports":
        return 200, {"items": planner.list_reports(_query(parsed, "period"))}
    if method == "GET" and path == "/report-corrections":
        period = _query(parsed, "period", "")
        if not period:
            raise ValidationError("period 不能为空")
        return 200, {"items": planner.list_corrections(period, _query(parsed, "status"))}
    if method == "GET" and path == "/trace/quota":
        quota_id = _query(parsed, "quota_id", "")
        if not quota_id:
            raise ValidationError("quota_id 不能为空")
        return 200, planner.trace_quota(quota_id)
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        path = self.path
        try:
            path = path.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        status, payload = route(self.service, self.command, path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动新职业培训名额与就业反馈规划服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = OutcomeService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
