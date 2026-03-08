"""Site Manager — site banning, promotion, and CRIC sync.

Manages site bans created automatically by the Error Handler (per-workflow)
or manually by operators (system-wide). Promotes per-workflow bans to
system-wide when a site is banned across enough workflows.

See docs/error_handling.md Section 6 for the full design.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from wms2.adapters.base import CRICAdapter, RucioAdapter
from wms2.config import Settings
from wms2.db.repository import Repository

logger = logging.getLogger(__name__)

# Module-level cache for _Temp RSE names (shared across SiteManager instances
# within the same process). Populated by sync_temp_rses(), used by
# get_temp_rse_sites() and has_temp_rse().
_temp_rse_cache: set[str] = set()

# Module-level cache for _Temp RSE PFN prefixes: {rse_name: "root://host:port/prefix"}
# Populated alongside _temp_rse_cache by sync_temp_rses().
_temp_rse_pfn_cache: dict[str, str] = {}


class SiteManager:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        cric_adapter: CRICAdapter | None = None,
        rucio_adapter: RucioAdapter | None = None,
    ):
        self.db = repository
        self.settings = settings
        self.cric_adapter = cric_adapter
        self.rucio_adapter = rucio_adapter

    async def ban_site(
        self,
        site_name: str,
        workflow_id=None,
        reason: str = "",
        failure_data: dict | None = None,
        duration_days: int | None = None,
    ):
        """Create a site ban. Returns the ban row, or None if site doesn't exist."""
        site = await self.db.get_site(site_name)
        if not site:
            logger.warning("Cannot ban unknown site %s — not in sites table", site_name)
            return None

        days = duration_days or self.settings.site_ban_duration_days
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)

        ban = await self.db.create_site_ban(
            site_name=site_name,
            workflow_id=workflow_id,
            reason=reason,
            failure_data=failure_data,
            expires_at=expires_at,
        )
        scope = f"workflow {workflow_id}" if workflow_id else "system-wide"
        logger.info(
            "Banned site %s (%s) for %d days: %s",
            site_name, scope, days, reason,
        )

        if workflow_id is not None:
            await self._check_promotion(site_name)

        return ban

    async def _check_promotion(self, site_name: str) -> None:
        """Promote per-workflow bans to system-wide if threshold is reached."""
        count = await self.db.count_active_workflow_bans_for_site(site_name)
        if count < self.settings.site_ban_promotion_threshold:
            return

        # Check for existing active system-wide ban
        existing = await self.db.get_active_bans_for_site(site_name)
        for ban in existing:
            if ban.workflow_id is None:
                logger.debug(
                    "System-wide ban already exists for %s, skipping promotion",
                    site_name,
                )
                return

        days = self.settings.site_ban_duration_days
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        await self.db.create_site_ban(
            site_name=site_name,
            workflow_id=None,
            reason=f"Promoted: {count} workflows banned",
            expires_at=expires_at,
        )
        logger.warning(
            "Promoted site %s to system-wide ban (%d workflow bans)",
            site_name, count,
        )

    async def get_banned_sites(self, workflow_id=None) -> list[str]:
        """Return deduplicated sorted list of banned site names."""
        system_bans = await self.db.get_active_system_bans()
        sites = {ban.site_name for ban in system_bans}

        if workflow_id is not None:
            wf_bans = await self.db.get_active_workflow_bans(workflow_id)
            sites.update(ban.site_name for ban in wf_bans)

        return sorted(sites)

    async def remove_ban(
        self, site_name: str, removed_by: str, workflow_id=None,
    ) -> int:
        """Remove active bans for a site. Returns count of bans removed."""
        return await self.db.remove_active_bans_for_site(
            site_name, removed_by, workflow_id=workflow_id,
        )

    async def get_active_bans(self, site_name: str | None = None):
        """Return active bans, optionally filtered by site."""
        if site_name:
            return await self.db.get_active_bans_for_site(site_name)
        return await self.db.list_all_active_bans()

    def get_temp_rse_sites(self) -> set[str]:
        """Return CMS site names that have a _Temp RSE.

        Strips both _Temp and _Disk suffixes to get bare CMS site names.
        E.g. "T2_CH_CERN_Temp" → "T2_CH_CERN",
             "T1_US_FNAL_Disk_Temp" → "T1_US_FNAL".
        """
        import re
        suffix_re = re.compile(r"(_Disk)?_Temp$")
        return {suffix_re.sub("", rse) for rse in _temp_rse_cache}

    def has_temp_rse(self, site: str) -> bool:
        """Check if a site has a _Temp RSE.

        Checks both {site}_Temp (T2) and {site}_Disk_Temp (T1) forms.
        """
        return self.get_temp_rse_name(site) is not None

    def get_temp_rse_name(self, site: str) -> str | None:
        """Return the actual _Temp RSE name for a site, or None.

        T2 sites use {site}_Temp, T1 sites use {site}_Disk_Temp.
        """
        if site.endswith("_Temp"):
            return site if site in _temp_rse_cache else None
        if f"{site}_Temp" in _temp_rse_cache:
            return f"{site}_Temp"
        if f"{site}_Disk_Temp" in _temp_rse_cache:
            return f"{site}_Disk_Temp"
        return None

    def get_temp_rse_pfn_prefix(self, rse: str) -> str | None:
        """Return the PFN prefix for a _Temp RSE, e.g. 'root://host:port/path/'.

        Used to construct PFN = pfn_prefix + lfn_suffix for non-deterministic RSEs.
        """
        return _temp_rse_pfn_cache.get(rse)

    async def sync_temp_rses(self) -> int:
        """Fetch _Temp RSE list and PFN prefixes from Rucio.

        Called during CRIC sync and at startup. Returns count of _Temp RSEs.
        Non-deterministic _Temp RSEs require explicit PFN for replica
        registration — we cache the root:// protocol prefix for each.
        """
        global _temp_rse_cache, _temp_rse_pfn_cache
        if self.rucio_adapter is None:
            logger.info("_Temp RSE sync skipped — no rucio_adapter configured")
            return 0
        try:
            rses = await self.rucio_adapter.list_temp_rses()
            _temp_rse_cache = set(rses)

            # Fetch PFN prefixes for each _Temp RSE
            pfn_cache: dict[str, str] = {}
            for rse in rses:
                try:
                    prefix = await self.rucio_adapter.get_rse_pfn_prefix(rse)
                    if prefix:
                        pfn_cache[rse] = prefix
                except Exception:
                    logger.warning("Failed to get PFN prefix for %s", rse)
            _temp_rse_pfn_cache = pfn_cache

            logger.info(
                "Cached %d _Temp RSEs (%d with PFN prefix) from Rucio",
                len(_temp_rse_cache), len(_temp_rse_pfn_cache),
            )
            return len(_temp_rse_cache)
        except Exception:
            logger.exception("Failed to fetch _Temp RSE list from Rucio")
            return 0

    async def sync_from_cric(self) -> dict[str, Any]:
        """Sync site information from CRIC into the sites table.

        Returns a stats dict: {"added": N, "updated": N, "total": N, "errors": N}.
        If no cric_adapter is configured, returns zeros and logs a warning.
        """
        if self.cric_adapter is None:
            logger.info("CRIC sync skipped — no cric_adapter configured")
            return {"added": 0, "updated": 0, "total": 0, "errors": 0}

        try:
            sites = await self.cric_adapter.get_sites()
        except Exception:
            logger.exception("CRIC sync failed — could not fetch sites")
            return {"added": 0, "updated": 0, "total": 0, "errors": 1}

        added = 0
        updated = 0
        errors = 0
        for site_data in sites:
            site_name = site_data.get("name")
            if not site_name:
                errors += 1
                continue
            try:
                existing = await self.db.get_site(site_name)
                await self.db.upsert_site(**site_data)
                if existing is None:
                    added += 1
                else:
                    updated += 1
            except Exception:
                logger.exception("Failed to upsert site %s", site_name)
                errors += 1

        logger.info(
            "CRIC sync complete: %d added, %d updated, %d total, %d errors",
            added, updated, len(sites), errors,
        )

        # Also refresh the _Temp RSE cache from Rucio
        await self.sync_temp_rses()

        return {"added": added, "updated": updated, "total": len(sites), "errors": errors}
