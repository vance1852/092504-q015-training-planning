"""规划服务的 HTTP/JSON 边界,复用基础服务的路由与处理器。"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from skills_workspace.api import Handler as BaseHandler
from skills_workspace.api import route as base_route
from skills_workspace.errors import DomainError, ValidationError
from skills_workspace.models import WriteReceipt

from .service import PlanningService
from .storage import PlanningDatabase


def _created(receipt: WriteReceipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _required(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def _planning_route(service: PlanningService, method: str, segments: list[str],
                    parsed, body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]] | None:
    query = parse_qs(parsed.query)
    if segments == ["planning", "demands"]:
        if method == "POST":
            return _created(service.submit_demand(actor_id=actor_id, **body))
        if method == "GET":
            return 200, {"items": service.list_demands(
                actor_id=actor_id, organization_id=_required(query, "organization_id"),
                occupation=query.get("occupation", [None])[0])}
    if (len(segments) == 4 and segments[:2] == ["planning", "demands"]
            and segments[3] == "verify" and method == "POST"):
        return _created(service.verify_demand(actor_id=actor_id,
                                              demand_key=segments[2], **body))
    if (len(segments) == 4 and segments[:2] == ["planning", "demands"]
            and segments[3] == "withdraw" and method == "POST"):
        return _created(service.withdraw_demand(actor_id=actor_id,
                                                demand_key=segments[2], **body))
    if segments == ["planning", "courses"] and method == "POST":
        return _created(service.register_course(actor_id=actor_id, **body))
    if segments == ["planning", "teachers"] and method == "POST":
        return _created(service.register_teacher(actor_id=actor_id, **body))
    if segments == ["planning", "equipment"] and method == "POST":
        return _created(service.register_equipment(actor_id=actor_id, **body))
    if segments == ["planning", "plan-generations"] and method == "POST":
        return _created(service.generate_plans(actor_id=actor_id, **body))
    if segments == ["planning", "plans"] and method == "GET":
        return 200, {"items": service.list_plans(
            actor_id=actor_id, organization_id=_required(query, "organization_id"),
            period_key=query.get("period_key", [None])[0])}
    if len(segments) == 3 and segments[:2] == ["planning", "plans"] and method == "GET":
        return 200, service.get_plan(actor_id=actor_id, plan_id=segments[2])
    if (len(segments) == 4 and segments[:2] == ["planning", "plans"]
            and segments[3] == "freeze" and method == "POST"):
        return _created(service.freeze_plan(actor_id=actor_id, plan_id=segments[2], **body))
    if (len(segments) == 4 and segments[:2] == ["planning", "plans"]
            and segments[3] == "approve" and method == "POST"):
        return _created(service.approve_plan(actor_id=actor_id, plan_id=segments[2], **body))
    if (len(segments) == 4 and segments[:2] == ["planning", "plans"]
            and segments[3] == "reject" and method == "POST"):
        return _created(service.reject_plan(actor_id=actor_id, plan_id=segments[2], **body))
    if segments == ["planning", "stat-periods"] and method == "POST":
        return _created(service.record_stat_period(actor_id=actor_id, **body))
    if segments == ["planning", "student-events"]:
        if method == "POST":
            return _created(service.record_student_event(actor_id=actor_id, **body))
        if method == "GET":
            return 200, {"items": service.list_student_events(
                actor_id=actor_id, organization_id=_required(query, "organization_id"),
                period_key=query.get("period_key", [None])[0])}
    if (len(segments) == 4 and segments[:2] == ["planning", "periods"]
            and segments[3] == "reports"):
        if method == "POST":
            return _created(service.publish_report(actor_id=actor_id,
                                                   period_key=segments[2], **body))
        if method == "GET":
            return 200, service.list_report_revisions(
                actor_id=actor_id, organization_id=_required(query, "organization_id"),
                period_key=segments[2])
    if segments == ["planning", "quotas"] and method == "GET":
        return 200, {"items": service.list_quotas(
            actor_id=actor_id, organization_id=_required(query, "organization_id"),
            period_key=query.get("period_key", [None])[0])}
    if (len(segments) == 4 and segments[:2] == ["planning", "quotas"]
            and segments[3] == "trace" and method == "GET"):
        return 200, service.trace_quota(actor_id=actor_id, quota_id=segments[2])
    if segments == ["planning", "impact-notices"] and method == "GET":
        return 200, {"items": service.list_impact_notices(
            actor_id=actor_id, organization_id=_required(query, "organization_id"),
            plan_id=query.get("plan_id", [None])[0])}
    return None


def route(service: PlanningService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把规划服务请求分派到领域服务,未命中时回落到基础服务路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if segments[:1] == ["planning"]:
            result = _planning_route(service, method, segments, parsed, body, actor_id)
            if result is not None:
                return result
        return base_route(service, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHandler):
    """使用规划路由的 HTTP 处理器。"""

    service: PlanningService
    router = staticmethod(route)


def main() -> int:
    """启动规划服务 HTTP 接口。"""

    parser = argparse.ArgumentParser(description="启动新职业培训名额与就业反馈规划服务")
    parser.add_argument("--database", default="planning.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = PlanningDatabase(args.database)
    Handler.service = PlanningService(database)
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
