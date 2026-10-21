"""Server-only provenance for automation; payload scope labels are not identity.

Only the existing central operator bearer is supported here. Do not distribute
it to SDKs. External emitters need a separately authorized, scope-bound adapter.
"""
from dataclasses import dataclass
import hmac
import json
import os
import re

IDENT = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
SCOPE_FIELDS = ("service_id", "tenant_id", "environment", "release", "source")


def scope_identifier(value):
    return value if isinstance(value, str) and IDENT.fullmatch(value) else None


@dataclass(frozen=True)
class VerifiedIngestIdentity:
    # This object is constructed by trusted server adapters, never from payloads.
    service_id: str
    tenant_id: str
    environment: str
    release: str
    source: str

    def bind(self, scope: dict) -> dict | None:
        claims = {key: getattr(self, key) for key in SCOPE_FIELDS}
        if all(scope_identifier(value) for value in claims.values()) and claims == scope:
            return {"issuer": "sofia_operator_bearer", "principal": "central-operator", "scope": claims}
        return None


def verified_operator_identity(authorization, event, config) -> VerifiedIngestIdentity | None:
    key = os.getenv("SOFIA_API_KEY", "")
    if not key or not isinstance(authorization, str) or not hmac.compare_digest(authorization.encode(), f"Bearer {key}".encode()):
        return None
    try:
        tags = json.loads(event.tags or "{}")
        scope = {"service_id": event.service_id, "tenant_id": tags["tenant_id"],
                 "environment": event.environment, "release": event.release, "source": event.source}
        if not all(scope_identifier(value) for value in scope.values()):
            return None
        repo = next(repo for repo in config.app_repos if repo.id == scope["service_id"])
        if (not repo.enabled or not repo.incident_team_enabled or scope["tenant_id"] not in repo.incident_tenant_ids or
            scope["environment"] not in repo.incident_environments):
            return None
        return VerifiedIngestIdentity(**scope)
    except (ValueError, TypeError, KeyError, StopIteration):
        return None


def identity_matches_context(context: dict) -> bool:
    identity = context.get("ingest_identity")
    if not isinstance(identity, dict):
        return False
    return (identity.get("issuer") == "sofia_operator_bearer" and identity.get("principal") == "central-operator" and
            identity.get("scope") == {key: context.get(key) for key in SCOPE_FIELDS} and
            all(scope_identifier(context.get(key)) for key in SCOPE_FIELDS))
