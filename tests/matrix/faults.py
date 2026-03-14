"""Fault injection for the test matrix.

After DAG files are generated and submitted, this module patches .sub files
to replace the real executable with a fault wrapper that exits with a code,
sends a signal, or delays.

DAGMan reads .sub files at node start time (not submission time), so patches
applied after condor_submit_dag are picked up correctly.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from tests.matrix.definitions import FaultSpec

logger = logging.getLogger(__name__)


def inject_faults(submit_dir: str, fault: FaultSpec) -> int:
    """Patch submit files in *submit_dir* according to *fault*.

    Returns the number of submit files patched.
    """
    submit_path = Path(submit_dir)
    patched = 0

    for group_dir in sorted(submit_path.glob("mg_*")):
        if fault.target == "proc":
            patched += _patch_proc_nodes(group_dir, fault)
        elif fault.target == "merge":
            patched += _patch_node(group_dir, "merge", fault)
        elif fault.target == "cleanup":
            patched += _patch_node(group_dir, "cleanup", fault)

    logger.info(
        "Injected fault (target=%s, exit=%d, sig=%d, retry_aware=%s) "
        "into %d submit files",
        fault.target, fault.exit_code, fault.signal, fault.retry_aware, patched,
    )
    return patched


def _patch_proc_nodes(group_dir: Path, fault: FaultSpec) -> int:
    """Patch processing node submit files within a merge group."""
    sub_files = sorted(group_dir.glob("proc_*.sub"))
    patched = 0
    patched_node_names: list[str] = []
    for idx, sub_file in enumerate(sub_files):
        if fault.node_indices is not None and idx not in fault.node_indices:
            continue
        if fault.retry_aware:
            # Save original before rewriting so fault_pre.sh can restore it
            orig_path = sub_file.with_suffix(".sub.orig")
            orig_path.write_text(sub_file.read_text())
            logger.debug("Saved original %s → %s", sub_file.name, orig_path.name)
        _rewrite_sub_file(sub_file, fault)
        patched_node_names.append(sub_file.stem)
        patched += 1

    if fault.retry_aware and patched_node_names:
        _write_fault_pre_script(group_dir / "fault_pre.sh")
        dag_file = group_dir / "group.dag"
        if dag_file.exists():
            _patch_dag_for_retry_aware(dag_file, set(patched_node_names))

    return patched


def _patch_node(group_dir: Path, node_name: str, fault: FaultSpec) -> int:
    """Patch a single named node (merge or cleanup)."""
    sub_file = group_dir / f"{node_name}.sub"
    if not sub_file.exists():
        logger.warning("Submit file not found: %s", sub_file)
        return 0
    _rewrite_sub_file(sub_file, fault)
    return 1


def _rewrite_sub_file(sub_file: Path, fault: FaultSpec) -> None:
    """Replace the executable in a .sub file with a fault wrapper script."""
    wrapper_path = sub_file.parent / f"{sub_file.stem}_fault.sh"
    _write_fault_wrapper(wrapper_path, fault)

    # Rewrite the submit file to use the wrapper
    lines = sub_file.read_text().splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("executable"):
            new_lines.append(f"executable = {wrapper_path}")
        elif stripped.startswith("arguments"):
            new_lines.append("arguments =")
        elif stripped.startswith("transfer_input_files"):
            # Drop transfer since wrapper is local
            continue
        else:
            new_lines.append(line)
    sub_file.write_text("\n".join(new_lines) + "\n")
    logger.debug("Patched %s → %s", sub_file, wrapper_path)


def _write_fault_wrapper(path: Path, fault: FaultSpec) -> None:
    """Write a small shell script that simulates the fault."""
    lines = ["#!/bin/bash", "# Fault injection wrapper"]

    if fault.delay_sec > 0:
        lines.append(f"sleep {fault.delay_sec}")

    if fault.signal > 0:
        lines.append(f"kill -{fault.signal} $$")
    elif fault.exit_code != 0:
        lines.append(f"exit {fault.exit_code}")
    else:
        # No fault actions means "skip output" — just exit 0
        lines.append("exit 0")

    path.write_text("\n".join(lines) + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_fault_pre_script(path: Path) -> None:
    """Write a PRE script that restores the original .sub on retry."""
    script = """\
#!/bin/bash
# fault_pre.sh — retry-aware PRE script for fault injection tests.
# On first attempt ($RETRY=0): delegates to pin_site.sh (fault wrapper runs).
# On retry ($RETRY>0): restores original .sub, then delegates to pin_site.sh.
RETRY_NUM=$1; shift
SUBMIT_FILE=$1
ELECTED_SITE=$2
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
if [ "$RETRY_NUM" -gt 0 ]; then
    ORIG="${SUBMIT_FILE}.orig"
    if [ -f "$ORIG" ]; then
        cp "$ORIG" "$SUBMIT_FILE"
    fi
fi
exec "$SCRIPT_DIR/pin_site.sh" "$SUBMIT_FILE" "$ELECTED_SITE"
"""
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.debug("Wrote fault_pre.sh at %s", path)


def _patch_dag_for_retry_aware(dag_path: Path, node_names: set[str]) -> None:
    """Replace pin_site.sh with fault_pre.sh $RETRY for faulted nodes."""
    lines = dag_path.read_text().splitlines()
    new_lines = []
    patched = 0
    for line in lines:
        # Match: SCRIPT PRE proc_000000 pin_site.sh ./proc_000000.sub T2_LOCAL_DEV
        if line.startswith("SCRIPT PRE "):
            parts = line.split()
            # parts: [SCRIPT, PRE, node_name, pin_site.sh, submit_file, elected_site]
            if len(parts) >= 4 and parts[2] in node_names and parts[3].endswith("pin_site.sh"):
                # Original: SCRIPT PRE node pin_site.sh sub site
                # Target:   SCRIPT PRE node fault_pre.sh $RETRY sub site
                parts[3] = "fault_pre.sh"
                parts.insert(4, "$RETRY")
                line = " ".join(parts)
                patched += 1
        new_lines.append(line)
    dag_path.write_text("\n".join(new_lines) + "\n")
    logger.debug("Patched %d PRE script lines in %s", patched, dag_path)
