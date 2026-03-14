"""Multi-schedd selection for DAG submission (DD-22)."""

import json
import logging
import random

logger = logging.getLogger(__name__)


def select_schedd(pool_json: str, fallback: str) -> str | None:
    """Capacity-weighted random selection from enabled schedds.

    Args:
        pool_json: JSON array of schedd configs, e.g.
            [{"name": "vocms047.cern.ch", "capacity": 100000, "enabled": true}]
            Empty string = single-schedd mode.
        fallback: Fallback schedd name (from settings.remote_schedd).

    Returns:
        Selected schedd name, or fallback if pool is empty/invalid.
    """
    if not pool_json:
        return fallback or None

    try:
        pool = json.loads(pool_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid schedd_pool JSON, falling back to remote_schedd")
        return fallback or None

    if not isinstance(pool, list) or not pool:
        return fallback or None

    enabled = [s for s in pool if s.get("enabled", True)]
    if not enabled:
        return fallback or None

    # Capacity-weighted random selection
    weights = [max(s.get("capacity", 1), 1) for s in enabled]
    selected = random.choices(enabled, weights=weights, k=1)[0]
    return selected["name"]
