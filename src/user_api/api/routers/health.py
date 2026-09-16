"""Liveness and readiness.

The two routes that do not require a token, because a load balancer cannot hold one.
Everything they report is therefore written on the assumption that a stranger is reading
it: process-wide counts and yes/no answers, never a key, a value, or an account.

There is deliberately no per-account number anywhere here. keyring learned this one the
expensive way -- a count on an unauthenticated endpoint that moves when one person does
something is an oracle, whatever it is counting.

``/healthy`` says only that this process is running, and it must never fail: an
orchestrator restarts a container whose liveness check fails, and restarting this process
does not fix keyring. ``/ready`` is where the database and keyring's keys are reported.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from user_api.api.dependencies import ContainerDep
from user_api.api.schemas.health import CheckResult, HealthResponse, LivenessResponse
from user_api.core.version import service_version

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


@router.get(
    "/healthy",
    operation_id="get_health",
    summary="Check that the service process is running",
    description=(
        "Liveness only. It reports nothing about the database or keyring, because a "
        "liveness check that failed when a dependency did would have an orchestrator "
        "restart a healthy process during somebody else's outage. Needs no token. Use "
        "`check_readiness` to find out whether requests will actually work."
    ),
    response_model=LivenessResponse,
)
async def get_health(container: ContainerDep) -> LivenessResponse:
    """Report that this process is alive, whatever else is not."""
    return LivenessResponse(
        status="alive",
        version=service_version(),
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
    )


@router.get(
    "/ready",
    operation_id="check_readiness",
    summary="Check that every dependency this service needs is usable",
    description=(
        "Reports the database and keyring's signing keys. Answers 200 when everything is "
        "usable and 503 when any check fails, with the same body shape either way. Needs "
        "no token, and it reports no personal data -- process-wide counts and yes/no "
        "answers only."
    ),
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def check_readiness(container: ContainerDep, response: Response) -> HealthResponse:
    """Check every dependency and summarize."""
    reachable, reason = await container.jwks.healthy()
    entries = await container.database.count("SELECT count(*) AS total FROM entries")

    checks = {
        "storage": CheckResult(
            status=STATUS_OK,
            # A process-wide total, not a per-account one. It moves when anybody writes,
            # which is what makes it useless as an oracle and still useful as a smoke test.
            detail={"entries": entries},
        ),
        "keyring": CheckResult(
            # Degraded rather than dead: the process is up, the database is fine, and
            # every authenticated request is failing with a 503 because a public key
            # cannot be fetched. Reporting that as healthy would hide the one outage this
            # service cannot work around.
            status=STATUS_OK if reachable else STATUS_DEGRADED,
            detail={
                "reachable": reachable,
                # A short reason, never a traceback and never the URL -- which may carry
                # userinfo, and would be handed to whoever can reach this endpoint.
                "reason": reason,
            },
        ),
    }

    healthy = all(check.status == STATUS_OK for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=STATUS_OK if healthy else STATUS_DEGRADED,
        version=service_version(),
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
        checks=checks,
    )
