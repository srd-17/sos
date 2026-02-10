# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit sys/devices/system/cpu/{online,present,possible} from vmcore (best-effort).
#
# Strategy:
# - Prefer drgn.helpers.linux.cpumask_to_cpus on cpu_*_mask symbols if present
# - Fallback to nr_cpu_ids for a conservative range "0-(nr_cpu_ids-1)"
# - If nothing is available, emit a stub with a clear header
#
# Output format:
# - Linux sysfs mask-style ranges, e.g. "0-3,8-11"
#
# Notes:
# - This module does not depend on drgn-tools; it uses drgn only.

from typing import Iterable, List

from sos.vmcore_report.emitters.registry import emits


def _ranges_to_str(vals: Iterable[int]) -> str:
    """Convert a sorted iterable of ints to Linux-style CPU list string."""
    arr = sorted(set(int(v) for v in vals))
    if not arr:
        return ""
    ranges: List[str] = []
    start = prev = arr[0]
    for cur in arr[1:]:
        if cur == prev + 1:
            prev = cur
            continue
        # flush previous range
        if start == prev:
            ranges.append(f"{start}")
        else:
            ranges.append(f"{start}-{prev}")
        start = prev = cur
    # flush last range
    if start == prev:
        ranges.append(f"{start}")
    else:
        ranges.append(f"{start}-{prev}")
    return ",".join(ranges)


def _cpumask_vals(prog, mask_symbol: str) -> List[int]:
    """Return list of CPUs from a cpumask symbol if available; else []"""
    try:
        from drgn.helpers.linux import cpumask_to_cpus  # type: ignore
        mask = prog[mask_symbol]
        return [int(c) for c in cpumask_to_cpus(mask)]
    except Exception:
        return []


def _nr_cpu_ids(prog) -> int:
    try:
        return int(prog["nr_cpu_ids"].value_())
    except Exception:
        return 0


def _best_effort_mask(prog) -> str:
    n = _nr_cpu_ids(prog)
    if n > 0:
        return f"0-{n-1}" if n > 1 else "0"
    return ""


@emits("sys/devices/system/cpu/online")
def emit_cpu_online(prog) -> str:
    vals = _cpumask_vals(prog, "cpu_online_mask")
    if vals:
        return _ranges_to_str(vals) + "\n"
    fallback = _best_effort_mask(prog)
    if fallback:
        return f"# vmcore-report: approximate (cpu_online_mask missing)\n{fallback}\n"
    return "# vmcore-report: stub (cpu_online_mask not reconstructable from vmcore)\n"


@emits("sys/devices/system/cpu/present")
def emit_cpu_present(prog) -> str:
    vals = _cpumask_vals(prog, "cpu_present_mask")
    if vals:
        return _ranges_to_str(vals) + "\n"
    # present ⊇ online; if present_mask missing, use online or fallback
    online = _cpumask_vals(prog, "cpu_online_mask")
    if online:
        return _ranges_to_str(online) + "\n"
    fallback = _best_effort_mask(prog)
    if fallback:
        return f"# vmcore-report: approximate (cpu_present_mask missing)\n{fallback}\n"
    return "# vmcore-report: stub (cpu_present_mask not reconstructable from vmcore)\n"


@emits("sys/devices/system/cpu/possible")
def emit_cpu_possible(prog) -> str:
    vals = _cpumask_vals(prog, "cpu_possible_mask")
    if vals:
        return _ranges_to_str(vals) + "\n"
    # possible ⊇ present; fallback as needed
    present = _cpumask_vals(prog, "cpu_present_mask")
    if present:
        return _ranges_to_str(present) + "\n"
    fallback = _best_effort_mask(prog)
    if fallback:
        return f"# vmcore-report: approximate (cpu_possible_mask missing)\n{fallback}\n"
    return "# vmcore-report: stub (cpu_possible_mask not reconstructable from vmcore)\n"
