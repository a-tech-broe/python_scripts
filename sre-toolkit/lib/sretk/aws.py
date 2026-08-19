"""AWS session handling, client caching, and pagination."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    raise SystemExit("boto3 is required:  pip install -r requirements.txt")

AwsError = (ClientError, BotoCoreError)


class Aws:
    """A region-pinned session with cached clients."""

    def __init__(self, region: str | None = None, profile: str | None = None):
        self.region = (region or os.environ.get("AWS_REGION")
                       or os.environ.get("AWS_DEFAULT_REGION"))
        if not self.region:
            raise ValueError("a region is required (--region, AWS_REGION)")
        self.profile = profile
        self.session = boto3.Session(profile_name=profile, region_name=self.region)
        self._cfg = Config(region_name=self.region,
                           retries={"max_attempts": 6, "mode": "standard"})
        self._clients: dict[str, Any] = {}
        self._identity: dict | None = None

    def client(self, service: str):
        if service not in self._clients:
            self._clients[service] = self.session.client(service, config=self._cfg)
        return self._clients[service]

    @property
    def identity(self) -> dict:
        if self._identity is None:
            self._identity = self.client("sts").get_caller_identity()
        return self._identity

    @property
    def account(self) -> str:
        return self.identity["Account"]

    def check(self) -> None:
        """Fail fast with a readable message if credentials are unusable."""
        try:
            self.identity  # noqa: B018 — triggers the STS call
        except (NoCredentialsError, *AwsError) as exc:
            raise RuntimeError(f"could not authenticate to AWS: {exc}") from exc

    def paginate(self, service: str, op: str, key: str, **kwargs) -> Iterator[dict]:
        client = self.client(service)
        if client.can_paginate(op):
            for page in client.get_paginator(op).paginate(**kwargs):
                yield from page.get(key, []) or []
        else:
            yield from getattr(client, op)(**kwargs).get(key, []) or []


def error_message(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Message", str(exc)).split("\n")[0]
    return str(exc).split("\n")[0]


def arn_tail(arn: str) -> str:
    """Last meaningful segment of an ARN (`.../my-thing` -> `my-thing`)."""
    return arn.rsplit("/", 1)[-1] if "/" in arn else arn.rsplit(":", 1)[-1]


def tag_value(tags: list[dict] | None, key: str = "Name", default: str = "") -> str:
    for tag in tags or []:
        if tag.get("Key") == key:
            return tag.get("Value", default)
    return default


def utc_window(window: timedelta) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return end - window, end
