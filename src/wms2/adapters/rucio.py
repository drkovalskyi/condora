"""Real Rucio adapter using native rucio.client.Client for CMS auth."""

import asyncio
import logging
import os
import re
from typing import Any

from .base import RucioAdapter

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1.0

# Strip _Disk/_Tape suffixes from RSE names to get CMS site names
_RSE_SUFFIX_RE = re.compile(r"_(Disk|Tape|Test|Temp)$")


class RucioClient(RucioAdapter):
    def __init__(self, base_url: str, account: str, cert_file: str, key_file: str, verify=True):
        self._base_url = base_url.rstrip("/")
        self._account = account
        self._cert_file = cert_file
        self._key_file = key_file
        self._native = None

    def _get_native_client(self):
        """Create/cache a native rucio.client.Client instance.

        Uses rucio.cfg for auth (account, proxy, auth_host). The account
        from config.py is intentionally NOT passed here — rucio.cfg
        controls which Rucio account authenticates.
        """
        if self._native is None:
            os.environ.setdefault("RUCIO_HOME", "/tmp/rucio")
            from rucio.client import Client as RucioNativeClient
            self._native = RucioNativeClient()
        return self._native

    async def _call_with_retry(self, func, *args, **kwargs):
        """Call a synchronous native client method with retry and backoff."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await asyncio.to_thread(func, *args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Rucio call %s failed (attempt %d/%d): %s, retrying in %.1fs",
                        func.__name__, attempt + 1, MAX_RETRIES, exc, wait,
                    )
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def close(self):
        pass  # native client doesn't need explicit cleanup

    @staticmethod
    def _rse_to_site(rse: str) -> str | None:
        """Convert RSE name to CMS site name, excluding tape-only RSEs."""
        if rse.endswith("_Tape"):
            return None
        return _RSE_SUFFIX_RE.sub("", rse)

    async def get_replicas(self, lfns: list[str]) -> dict[str, list[str]]:
        client = self._get_native_client()
        dids = [{"scope": "cms", "name": lfn} for lfn in lfns]
        replicas = await asyncio.to_thread(
            lambda: list(client.list_replicas(dids, schemes=["root"]))
        )
        result: dict[str, list[str]] = {lfn: [] for lfn in lfns}
        for entry in replicas:
            lfn = entry.get("name", "")
            rses = entry.get("rses", {})
            sites = set()
            for rse_name in rses:
                site = self._rse_to_site(rse_name)
                if site:
                    sites.add(site)
            if lfn in result:
                result[lfn] = sorted(sites)
        return result

    async def create_rule(self, dataset: str, destination: str,
                          scope: str = "cms", **kwargs: Any) -> str:
        client = self._get_native_client()
        dids = [{"scope": scope, "name": dataset}]

        def _add_rule():
            rule_ids = client.add_replication_rule(
                dids, copies=1, rse_expression=destination, **kwargs,
            )
            return rule_ids[0] if rule_ids else ""

        return await self._call_with_retry(_add_rule)

    async def get_rule_status(self, rule_id: str) -> dict[str, Any]:
        client = self._get_native_client()
        return await self._call_with_retry(client.get_replication_rule, rule_id)

    async def delete_rule(self, rule_id: str) -> None:
        client = self._get_native_client()
        await self._call_with_retry(client.delete_replication_rule, rule_id)

    @staticmethod
    def site_to_disk_rse(site_name: str) -> str:
        """Convert CMS site name to disk RSE name (e.g. T2_CH_CERN → T2_CH_CERN_Disk)."""
        if site_name.endswith(("_Disk", "_Tape", "_Test", "_Temp")):
            return site_name
        return f"{site_name}_Disk"

    async def register_replicas(self, rse: str, files: list[dict[str, Any]]) -> None:
        client = self._get_native_client()
        await self._call_with_retry(client.add_replicas, rse=rse, files=files)

    async def add_did(self, scope: str, name: str, did_type: str = "DATASET") -> None:
        client = self._get_native_client()

        def _add_did():
            try:
                if did_type == "CONTAINER":
                    client.add_container(scope=scope, name=name)
                else:
                    client.add_dataset(scope=scope, name=name)
            except Exception as e:
                # DataIdentifierAlreadyExists — idempotent
                if "already exists" in str(e).lower() or "SCOPE_NAME_ALREADY_EXISTS" in str(e):
                    return
                raise

        await self._call_with_retry(_add_did)

    async def attach_dids(self, scope: str, name: str,
                          dids: list[dict[str, str]]) -> None:
        client = self._get_native_client()

        def _attach():
            try:
                client.attach_dids(scope=scope, name=name, dids=dids)
            except Exception as e:
                # DuplicateContent / FileAlreadyExists / already attached — idempotent
                err_str = str(e).lower()
                if ("already exists" in err_str or "duplicate" in err_str
                        or "already added" in err_str):
                    return
                raise

        await self._call_with_retry(_attach)

    async def get_available_pileup_files(self, dataset: str,
                                         preferred_rses: list[str] | None = None) -> list[str]:
        """Get LFNs with on-disk replicas using rucio-clients Python API."""
        return await asyncio.to_thread(self._get_pileup_sync, dataset, preferred_rses)

    def _get_pileup_sync(self, dataset: str,
                         preferred_rses: list[str] | None = None) -> list[str]:
        from rucio.client import Client as RucioNativeClient

        rucio_home = os.environ.get("RUCIO_HOME", "/tmp/rucio")
        os.environ.setdefault("RUCIO_HOME", rucio_home)
        c = RucioNativeClient()

        # Build RSE match set: both bare site name and _Disk suffix
        rse_filter: set[str] | None = None
        if preferred_rses:
            rse_filter = set()
            for rse in preferred_rses:
                rse_filter.add(rse)
                if not rse.endswith("_Disk"):
                    rse_filter.add(rse + "_Disk")

        available = []
        for replica in c.list_replicas(
            [{"scope": "cms", "name": dataset}],
            schemes=["root"],
        ):
            states = replica.get("states", {})
            disk_rses = [rse for rse in states if not rse.endswith("_Tape")]
            if rse_filter:
                if any(rse in rse_filter for rse in disk_rses):
                    available.append(replica["name"])
            else:
                if disk_rses:
                    available.append(replica["name"])
        return available
