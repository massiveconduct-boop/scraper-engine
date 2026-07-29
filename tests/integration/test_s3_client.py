# tests/integration/test_s3_client.py
"""S3Client integration tests — real MinIO, BD-07 retention tagging.

Uses the live docker-compose MinIO instance. The shared "scraper-snapshots"
bucket is used for the normal read/write/tag paths (it already exists,
created by the running api/worker containers); a throwaway per-test bucket
is used only to exercise _ensure_bucket's create-on-missing branch, so we
never touch the bucket the live containers depend on.
"""

import uuid

import boto3
import pytest
from botocore.config import Config

from scraper_engine.core.tenant import TenantId
from scraper_engine.storage.s3_client import S3Client

_ENDPOINT = "http://localhost:9000"
_ACCESS_KEY = "minioadmin"
_SECRET_KEY = "minioadmin"
_BUCKET = "scraper-snapshots"


def _raw_client():
    return boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )


@pytest.fixture
async def s3():
    client = S3Client(
        endpoint_url=_ENDPOINT,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        bucket=_BUCKET,
    )
    await client.start()
    yield client
    await client.stop()


@pytest.mark.integration
class TestS3ClientUnstarted:
    async def test_store_snapshot_before_start_raises(self) -> None:
        client = S3Client(_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, _BUCKET)
        with pytest.raises(
            RuntimeError, match=r"start\(\) must be called before store_snapshot\(\)"
        ):
            await client.store_snapshot(
                TenantId("system"), "job1", "http://x", "<html></html>", True
            )

    async def test_ping_before_start_raises(self) -> None:
        client = S3Client(_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, _BUCKET)
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before ping\(\)"):
            await client.ping()

    async def test_get_snapshot_before_start_raises(self) -> None:
        client = S3Client(_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, _BUCKET)
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before get_snapshot\(\)"):
            await client.get_snapshot("some/key.html")

    async def test_apply_lifecycle_policy_before_start_is_noop(self) -> None:
        client = S3Client(_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, _BUCKET)
        await client.apply_lifecycle_policy()

    async def test_ensure_bucket_before_start_is_noop(self) -> None:
        client = S3Client(_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, _BUCKET)
        await client._ensure_bucket()


@pytest.mark.integration
class TestS3ClientConnected:
    async def test_start_ensures_bucket_and_applies_lifecycle(self, s3: S3Client) -> None:
        assert s3._client is not None
        raw = _raw_client()
        lifecycle = raw.get_bucket_lifecycle_configuration(Bucket=_BUCKET)
        rule_ids = {r["ID"] for r in lifecycle["Rules"]}
        assert {"expire-successful-snapshots", "expire-failed-snapshots"} <= rule_ids

    async def test_ping_succeeds_against_live_bucket(self, s3: S3Client) -> None:
        await s3.ping()

    async def test_store_and_get_snapshot_success(self, s3: S3Client) -> None:
        tenant = TenantId("system")
        key = await s3.store_snapshot(
            tenant, "job-success", "http://example.com", "<html>ok</html>", True
        )
        assert key.startswith(f"snapshots/{tenant}/job-success/")

        content = await s3.get_snapshot(key)
        assert content == "<html>ok</html>"

        raw = _raw_client()
        tagging = raw.get_object_tagging(Bucket=_BUCKET, Key=key)
        tags = {t["Key"]: t["Value"] for t in tagging["TagSet"]}
        assert tags["retention"] == "success"
        raw.delete_object(Bucket=_BUCKET, Key=key)

    async def test_store_snapshot_failed_tags_as_failed(self, s3: S3Client) -> None:
        tenant = TenantId("system")
        key = await s3.store_snapshot(
            tenant, "job-fail", "http://example.com", "<html>err</html>", False
        )
        raw = _raw_client()
        tagging = raw.get_object_tagging(Bucket=_BUCKET, Key=key)
        tags = {t["Key"]: t["Value"] for t in tagging["TagSet"]}
        assert tags["retention"] == "failed"
        raw.delete_object(Bucket=_BUCKET, Key=key)

    async def test_get_snapshot_missing_key_returns_none(self, s3: S3Client) -> None:
        result = await s3.get_snapshot(f"snapshots/nonexistent/{uuid.uuid4()}.html")
        assert result is None


@pytest.mark.integration
class TestS3ClientBucketCreation:
    """Exercises the create_bucket path in _ensure_bucket. The shared
    scraper-snapshots bucket (owned by the live containers) always already
    exists, so the create-on-missing branch needs its own throwaway bucket."""

    async def test_start_creates_bucket_when_absent(self) -> None:
        bucket = f"s3client-test-{uuid.uuid4().hex[:12]}"
        raw = _raw_client()
        client = S3Client(_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, bucket)
        try:
            await client.start()
            raw.head_bucket(Bucket=bucket)  # raises if creation didn't happen
        finally:
            raw.delete_bucket(Bucket=bucket)
