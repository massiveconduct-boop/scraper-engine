# storage/s3_client.py
"""S3/MinIO client for HTML snapshot storage.

BD-07 retention policy:
  - Failed snapshots: retain 30 days (for debugging)
  - Successful snapshots: retain 1 day (transient, content already extracted)
  - Automated deletion via S3 lifecycle rules
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId


class S3Client:
    """S3/MinIO client for persisting raw HTML snapshots."""

    # BD-07 retention
    FAILED_RETENTION_DAYS = 30
    SUCCESS_RETENTION_DAYS = 1

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        self._endpoint = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._client: Any | None = None

    async def start(self) -> None:
        """Initialize S3 client, ensure bucket exists, apply lifecycle policies."""
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=Config(signature_version="s3v4"),
        )
        await self._ensure_bucket()
        await self.apply_lifecycle_policy()

    async def stop(self) -> None:
        """Close the S3 client."""
        self._client = None

    async def store_snapshot(
        self,
        tenant_id: TenantId,
        job_id: str,
        url: str,
        html: str,
        success: bool,
    ) -> str:
        """Store an HTML snapshot. Returns the object key."""
        if self._client is None:
            raise RuntimeError("S3Client.start() must be called before store_snapshot()")

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        key = f"snapshots/{tenant_id}/{job_id}/{timestamp}.html"

        retention_tag = "success" if success else "failed"
        tags = {
            "retention": retention_tag,
            "tenant": str(tenant_id),
        }

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
            Tagging="&".join(f"{k}={v}" for k, v in tags.items()),
        )
        return key

    async def ping(self) -> None:
        """Raise if the bucket isn't reachable. Used by the composite health check —
        distinct from get_snapshot(), which returns None for a missing key (expected)
        and would mask a real connectivity failure as "not found"."""
        if self._client is None:
            raise RuntimeError("S3Client.start() must be called before ping()")
        self._client.head_bucket(Bucket=self._bucket)

    async def get_snapshot(self, key: str) -> str | None:
        """Retrieve a previously stored snapshot by key."""
        if self._client is None:
            raise RuntimeError("S3Client.start() must be called before get_snapshot()")

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
            return body.decode("utf-8")
        except Exception:
            return None

    async def apply_lifecycle_policy(self) -> None:
        """Apply BD-07 retention lifecycle rules to the bucket."""
        if self._client is None:
            return

        policy = {
            "Rules": [
                {
                    "ID": "expire-successful-snapshots",
                    "Filter": {"Tag": {"Key": "retention", "Value": "success"}},
                    "Status": "Enabled",
                    "Expiration": {"Days": self.SUCCESS_RETENTION_DAYS},
                },
                {
                    "ID": "expire-failed-snapshots",
                    "Filter": {"Tag": {"Key": "retention", "Value": "failed"}},
                    "Status": "Enabled",
                    "Expiration": {"Days": self.FAILED_RETENTION_DAYS},
                },
            ]
        }
        self._client.put_bucket_lifecycle_configuration(
            Bucket=self._bucket,
            # boto3 serializes this itself (S3's wire format is XML, not JSON) —
            # it needs the dict, not a pre-serialized string. This path was never
            # exercised before S3Client was wired into api/main.py's lifespan (it
            # was fully dead code previously), so the bug was latent.
            LifecycleConfiguration=policy,
        )

    async def _ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist."""
        if self._client is None:
            return
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            self._client.create_bucket(Bucket=self._bucket)
