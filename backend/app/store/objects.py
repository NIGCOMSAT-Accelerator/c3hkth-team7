"""S3-compatible object storage (MinIO).

Everything that is a blob rather than a row: advisory voice notes, cached
imagery crops, operator exports. Self-hosted MinIO keeps the same sovereignty
property the NIGCOMSAT layer exists for — no third party sits between the
warning and the recipient.

**STATUS — partially live.** `imagery_key` + `put` are used: the stateful Scout
caches each imagery discovery here, and `source_poll_state.cache_key` holds the
pointer.

**STATUS — no callers yet** for `put_audio` and `audio_url` (nothing synthesises
speech — there is no TTS module) or `export_key` (nothing generates exports). Those are the storage
halves of two features whose producing halves are not built — deliberate, since the
write path is what a TTS stage would otherwise have to invent, and
`alerts.audio_key` already exists to hold the result. Do not read a call to
`put_audio` as evidence that voice notes ship; check for a producer first.
`tests/test_schema_contract.py` tracks which is which.

**The MinIO SDK is synchronous**, so every call here is pushed to a worker
thread with `asyncio.to_thread`. Same pattern as `eo/cog.py` for rasterio and
`dispatch/email_channel.py` for smtplib, for the same reason: the pipeline runs
on one event loop and a blocking socket read would stall every other stage.

**Three contracts:**

1. **Unconfigured is not an error.** Without credentials `available` is False
   and every operation returns None / False. Voice notes are an enhancement; a
   missing object store must not stop a text advisory going out. This mirrors
   how the dispatchers treat a missing channel credential.

2. **Keys are durable, URLs are not.** We persist the object *key*
   (`alerts.audio_key`) and mint a presigned URL per request. A stored URL would
   expire and leave a dead link in the database.

3. **Buckets are private.** Nothing is world-readable. Audio is addressable by a
   phone number's owner, so a public bucket would leak who was warned about what.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import dataclass
from datetime import timedelta

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_client = None
_checked_buckets: set[str] = set()


@dataclass(frozen=True)
class StoredObject:
    """The result of a successful upload."""

    bucket: str
    key: str
    size_bytes: int
    content_type: str
    etag: str | None = None


def available() -> bool:
    """True when credentials are configured.

    Deliberately does not test connectivity — that would make a property access
    do network I/O. `/health` calls `ping()` for the live check.
    """
    return bool(settings.s3_access_key and settings.s3_secret_key)


def _get_client():
    """Build (once) the MinIO client. None when unconfigured."""
    global _client
    if not available():
        return None
    if _client is None:
        from minio import Minio

        _client = Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
            region=settings.s3_region,
        )
        log.info(
            "object store client ready",
            extra={"endpoint": settings.s3_endpoint, "secure": settings.s3_secure},
        )
    return _client


# --------------------------------------------------------------------------- #
# Buckets
# --------------------------------------------------------------------------- #


def _ensure_bucket_sync(bucket: str) -> None:
    """Create the bucket if missing. Blocking; call via `_ensure_bucket`."""
    client = _get_client()
    if client is None:
        return
    if bucket in _checked_buckets:
        return
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket, location=settings.s3_region)
        log.info("created bucket", extra={"bucket": bucket})
    _checked_buckets.add(bucket)


async def _ensure_bucket(bucket: str) -> None:
    if not settings.s3_auto_create_buckets or bucket in _checked_buckets:
        return
    try:
        await asyncio.to_thread(_ensure_bucket_sync, bucket)
    except Exception as exc:
        # Non-fatal: the upload that follows will fail with a clearer error, and
        # a deployment where buckets are pre-created by ops has no permission to
        # create them anyway.
        log.warning("bucket check failed", extra={"bucket": bucket, "error": str(exc)})


async def ensure_buckets() -> list[str]:
    """Pre-create every configured bucket. Called once at startup.

    Doing this at boot rather than on first write means a misconfigured endpoint
    shows up in the startup log, not during the first flood.
    """
    if not available():
        log.info("object store not configured; blob features disabled")
        return []

    buckets = [
        settings.s3_bucket_audio,
        settings.s3_bucket_imagery,
        settings.s3_bucket_exports,
    ]
    for bucket in buckets:
        await _ensure_bucket(bucket)
    return sorted(_checked_buckets)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def audio_key(alert_id: str, language: str) -> str:
    """Key for an advisory voice note.

    Language is in the key because one alert can be voiced in several languages —
    a cooperative officer reading Hausa and a district office reading English
    share an assessment but not an audio file.
    """
    return f"advisory/{alert_id}/{language}.mp3"


def imagery_key(scene_id: str, aoi_id: str, band: str, ext: str = "tif") -> str:
    """Key for a cached windowed COG crop.

    Keyed on scene + AOI + band, which is exactly the tuple that determines the
    pixels. Two AOIs overlapping the same scene get separate objects; the same
    AOI re-read from the same scene hits cache.
    """
    # Scene IDs contain characters that are awkward in keys, so hash rather than
    # sanitise — collisions are not a practical concern at 12 hex chars here and
    # the mapping stays stable across runs.
    digest = hashlib.sha256(scene_id.encode()).hexdigest()[:12]
    return f"crops/{aoi_id}/{digest}/{band}.{ext}"


def export_key(subscriber_id: str, name: str) -> str:
    return f"exports/{subscriber_id}/{name}"


# --------------------------------------------------------------------------- #
# Put / get
# --------------------------------------------------------------------------- #


def _put_sync(bucket: str, key: str, data: bytes, content_type: str):
    client = _get_client()
    if client is None:
        return None
    return client.put_object(
        bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


async def put(
    bucket: str, key: str, data: bytes, *, content_type: str = "application/octet-stream"
) -> StoredObject | None:
    """Upload bytes. Returns None when unconfigured or on failure.

    Never raises — a failed audio upload must degrade to a text-only alert, not
    break the dispatch fan-out.
    """
    if not available():
        return None

    await _ensure_bucket(bucket)

    try:
        result = await asyncio.to_thread(_put_sync, bucket, key, data, content_type)
    except Exception as exc:
        log.warning(
            "object upload failed",
            extra={"bucket": bucket, "key": key, "error": str(exc)},
        )
        return None

    if result is None:
        return None

    log.info(
        "object stored",
        extra={"bucket": bucket, "key": key, "bytes": len(data)},
    )
    return StoredObject(
        bucket=bucket,
        key=key,
        size_bytes=len(data),
        content_type=content_type,
        etag=getattr(result, "etag", None),
    )


async def put_audio(alert_id: str, language: str, mp3: bytes) -> StoredObject | None:
    """Store an advisory voice note."""
    return await put(
        settings.s3_bucket_audio,
        audio_key(alert_id, language),
        mp3,
        content_type="audio/mpeg",
    )


def _get_sync(bucket: str, key: str) -> bytes | None:
    client = _get_client()
    if client is None:
        return None
    response = None
    try:
        response = client.get_object(bucket, key)
        return response.read()
    finally:
        # The SDK requires both, and leaking either exhausts the connection pool
        # after a few hundred reads.
        if response is not None:
            response.close()
            response.release_conn()


async def get(bucket: str, key: str) -> bytes | None:
    """Download bytes. None when absent, unconfigured, or on failure."""
    if not available():
        return None
    try:
        return await asyncio.to_thread(_get_sync, bucket, key)
    except Exception as exc:
        log.debug(
            "object read failed", extra={"bucket": bucket, "key": key, "error": str(exc)}
        )
        return None


def _exists_sync(bucket: str, key: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.stat_object(bucket, key)
        return True
    except Exception:
        return False


async def exists(bucket: str, key: str) -> bool:
    if not available():
        return False
    try:
        return await asyncio.to_thread(_exists_sync, bucket, key)
    except Exception:
        return False


async def delete(bucket: str, key: str) -> bool:
    if not available():
        return False

    def _delete_sync() -> bool:
        client = _get_client()
        if client is None:
            return False
        client.remove_object(bucket, key)
        return True

    try:
        return await asyncio.to_thread(_delete_sync)
    except Exception as exc:
        log.warning(
            "object delete failed",
            extra={"bucket": bucket, "key": key, "error": str(exc)},
        )
        return False


# --------------------------------------------------------------------------- #
# Presigned URLs
# --------------------------------------------------------------------------- #


def _presign_sync(bucket: str, key: str, ttl: int) -> str | None:
    client = _get_client()
    if client is None:
        return None
    return client.presigned_get_object(bucket, key, expires=timedelta(seconds=ttl))


async def presigned_url(
    bucket: str, key: str, *, ttl_seconds: int | None = None
) -> str | None:
    """Time-limited download URL.

    This is how audio reaches a subscriber: Telegram's `sendVoice` and WhatsApp's
    media send both accept a URL, and a short-lived signed one means the file is
    never world-readable. Minted per request — never stored, because it expires.
    """
    if not available():
        return None
    ttl = ttl_seconds or settings.s3_presign_ttl_seconds
    try:
        return await asyncio.to_thread(_presign_sync, bucket, key, ttl)
    except Exception as exc:
        log.warning(
            "presign failed", extra={"bucket": bucket, "key": key, "error": str(exc)}
        )
        return None


async def audio_url(alert_id: str, language: str) -> str | None:
    """Presigned URL for an advisory voice note."""
    return await presigned_url(settings.s3_bucket_audio, audio_key(alert_id, language))


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def ping() -> bool:
    """Live connectivity check. False when unconfigured or unreachable."""
    if not available():
        return False

    def _ping_sync() -> bool:
        client = _get_client()
        if client is None:
            return False
        # Cheapest authenticated round trip the SDK offers.
        client.list_buckets()
        return True

    try:
        return await asyncio.to_thread(_ping_sync)
    except Exception as exc:
        log.debug("object store ping failed", extra={"error": str(exc)})
        return False
