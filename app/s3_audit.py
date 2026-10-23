"""Audit-trail export to AWS S3 (or LocalStack).

The financial ledger in PostgreSQL is the source of truth; S3 is the
**archival** tier.  Every export:

* selects the usage events of a tenant (optionally bounded by a time window);
* serialises them as newline-delimited JSON (NDJSON);
* gzip-compresses the payload;
* uploads it to ``s3://<bucket>/<prefix>/<tenant>/<YYYY>/<MM>/<ts>.jsonl.gz``.

LocalStack is fully supported: point ``S3_ENDPOINT_URL`` at
``http://localhost:4566`` and the exporter uses path-style addressing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import io
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.models import Tenant, UsageEvent
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.s3_audit")

#: Serialised form of one ledger row.
Row = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuditArchiveResult:
    """Outcome of a single audit export."""

    bucket: str
    key: str
    records: int
    raw_bytes: int
    compressed_bytes: int
    etag: str | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "records": self.records,
            "raw_bytes": self.raw_bytes,
            "compressed_bytes": self.compressed_bytes,
            "etag": self.etag,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def serialize_row(event: UsageEvent) -> Row:
    """Convert a ledger row into a JSON-serialisable dictionary."""
    return {
        "id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "request_id": event.request_id,
        "idempotency_key": event.idempotency_key,
        "model": event.model,
        "provider": event.provider,
        "input_tokens": event.input_tokens,
        "cached_input_tokens": event.cached_input_tokens,
        "output_tokens": event.output_tokens,
        "reasoning_tokens": event.reasoning_tokens,
        "total_tokens": event.total_tokens,
        "cost_microcents": event.cost_microcents,
        "currency": event.currency,
        "status": event.status,
        "latency_ms": event.latency_ms,
        "is_anomaly": event.is_anomaly,
        "anomaly_type": event.anomaly_type,
        "metadata": event.metadata_ or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def build_payload(rows: list[Row]) -> tuple[bytes, bytes]:
    """Return ``(raw_bytes, gzip_bytes)`` for the NDJSON payload."""
    raw = io.BytesIO()
    for row in rows:
        raw.write(json.dumps(row, separators=(",", ":"), default=str).encode("utf-8"))
        raw.write(b"\n")
    raw_bytes = raw.getvalue()
    compressed = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
    return raw_bytes, compressed


class AuditArchiver:
    """Exports the usage ledger to S3 / LocalStack."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = client

    # -- client -------------------------------------------------------------
    @property
    def client(self) -> Any:
        """Lazily build the boto3 S3 client."""
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self._settings.s3_endpoint_url,
                region_name=self._settings.aws_region,
                aws_access_key_id=self._settings.aws_access_key_id,
                aws_secret_access_key=self._settings.aws_secret_access_key,
                config=Config(
                    s3={"addressing_style": self._settings.s3_addressing_style},
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    def ensure_bucket(self, bucket: str | None = None) -> str:
        """Create the bucket when it does not exist yet (idempotent)."""
        name = bucket or self._settings.s3_bucket
        try:
            self.client.head_bucket(Bucket=name)
            return name
        except Exception:  # noqa: BLE001 - any error means "create it"
            pass
        kwargs: dict[str, Any] = {"Bucket": name}
        region = self._settings.aws_region
        if region and region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            self.client.create_bucket(**kwargs)
        except Exception:  # noqa: BLE001 - a concurrent creator won the race
            logger.debug("bucket %s already exists", name)
        return name

    # -- export -------------------------------------------------------------
    async def archive_usage_ledger_to_s3(
        self,
        session: AsyncSession,
        *,
        tenant_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        bucket: str | None = None,
        prefix: str | None = None,
        label: str | None = None,
    ) -> AuditArchiveResult:
        """Archive the usage ledger to S3 and return the upload result."""
        if not self._settings.audit_enabled:
            raise RuntimeError("audit export is disabled by configuration")

        statement = select(UsageEvent).order_by(UsageEvent.created_at.asc())
        if tenant_id:
            statement = statement.where(
                UsageEvent.tenant_id.in_(await self._tenant_ids(session, tenant_id))
            )
        if since is not None:
            statement = statement.where(UsageEvent.created_at >= since)
        if until is not None:
            statement = statement.where(UsageEvent.created_at < until)

        events = (await session.execute(statement)).scalars().all()
        rows = [serialize_row(event) for event in events]
        raw_bytes, compressed = build_payload(rows)

        now = datetime.now(UTC)
        bucket_name = bucket or self._settings.s3_bucket
        key_prefix = (prefix or self._settings.s3_prefix).strip("/")
        scope = label or tenant_id or "all"
        key = f"{key_prefix}/{scope}/{now:%Y/%m}/{now:%Y%m%dT%H%M%SZ}-{len(rows)}rows.jsonl.gz"

        etag = await asyncio.to_thread(self._upload, bucket_name, key, compressed, rows)
        metrics.AUDIT_RECORDS_UPLOADED_TOTAL.inc(len(rows))
        logger.info("archived %d usage events to s3://%s/%s", len(rows), bucket_name, key)
        return AuditArchiveResult(
            bucket=bucket_name,
            key=key,
            records=len(rows),
            raw_bytes=len(raw_bytes),
            compressed_bytes=len(compressed),
            etag=etag,
            created_at=now,
        )

    async def _tenant_ids(self, session: AsyncSession, reference: str) -> list[Any]:
        """Resolve a tenant reference (UUID or slug) into a list of ids."""
        from uuid import UUID

        try:
            return [UUID(reference)]
        except ValueError:
            rows = (
                (await session.execute(select(Tenant.id).where(Tenant.slug == reference)))
                .scalars()
                .all()
            )
            return list(rows)

    def _upload(self, bucket: str, key: str, payload: bytes, rows: list[Row]) -> str | None:
        """Blocking upload executed on a worker thread."""
        self.ensure_bucket(bucket)
        response = self.client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
            Metadata={"record-count": str(len(rows))},
        )
        etag = response.get("ETag") if isinstance(response, dict) else None
        return etag.strip('"') if isinstance(etag, str) else None


async def archive_usage_ledger_to_s3(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    bucket: str | None = None,
    prefix: str | None = None,
    settings: Settings | None = None,
) -> AuditArchiveResult:
    """Module-level convenience wrapper around :meth:`AuditArchiver`."""
    archiver = AuditArchiver(settings)
    return await archiver.archive_usage_ledger_to_s3(
        session,
        tenant_id=tenant_id,
        since=since,
        until=until,
        bucket=bucket,
        prefix=prefix,
    )
