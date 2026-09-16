"""The health payloads.

Two of them, because liveness and readiness answer different questions for different
readers: an orchestrator deciding whether to restart this process, and a load balancer
deciding whether to send it traffic.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LivenessResponse(BaseModel):
    """What ``GET /healthy`` returns.

    Deliberately says nothing about dependencies, and deliberately cannot fail. This
    service shipped a liveness endpoint that returned 500 when an unanticipated JWKS
    failure escaped, which is the worst bug this particular endpoint can have: an
    orchestrator reads 500 as "this process is broken" and restarts a process that was
    working perfectly, during an outage of a different service, repeatedly.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "alive",
                    "version": "0.1.0",
                    "environment": "local",
                    "uptime_seconds": 12.34,
                }
            ]
        }
    )

    status: str = Field(description="Always 'alive'. This endpoint does no I/O and never fails.")
    version: str = Field(description="Running version of the service.")
    environment: str = Field(description="Which deployment this is.")
    uptime_seconds: float = Field(description="Seconds since the process started serving.")


class CheckResult(BaseModel):
    """One dependency's contribution to readiness."""

    status: str = Field(description="'ok' or 'degraded'.")
    detail: dict[str, object] = Field(
        default_factory=dict, description="Check-specific facts, such as whether keyring answered."
    )


class HealthResponse(BaseModel):
    """What ``GET /ready`` returns.

    Reported at both levels deliberately: the top-level status is what a load balancer
    reads, the per-check detail is what a human reads at three in the morning.

    Nothing here is sensitive. This endpoint requires no token, so every field on it is
    written on the assumption that a stranger can read it -- counts and yes/no answers,
    never a key, a value, or a path. In particular it reports no per-account anything: a
    count that moved when one person wrote something would be an oracle on an
    unauthenticated endpoint, which is exactly the mistake keyring made once and documented.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "version": "0.1.0",
                    "environment": "local",
                    "uptime_seconds": 12.34,
                    "checks": {
                        "keyring": {"status": "ok", "detail": {"reachable": True}},
                        "storage": {"status": "ok", "detail": {"entries": 52}},
                    },
                }
            ]
        }
    )

    status: str = Field(description="'ok' when every check passed, otherwise 'degraded'.")
    version: str = Field(description="Running version of the service.")
    environment: str = Field(description="Which deployment this is.")
    uptime_seconds: float = Field(description="Seconds since the process started serving.")
    checks: dict[str, CheckResult] = Field(description="Per-dependency results.")
