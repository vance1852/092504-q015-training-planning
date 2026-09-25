"""新职业培训名额与就业反馈规划服务的服务端基础包。"""

from .outcomes import OutcomeService
from .planning import PlanningService
from .service import DomainService

__all__ = ["DomainService", "PlanningService", "OutcomeService"]
