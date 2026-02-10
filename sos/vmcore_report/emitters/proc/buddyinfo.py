# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/buddyinfo reconstructed from vmcore via drgn.
#
# Based on drgn-tools buddyinfo module logic, but implemented without a runtime
# dependency on drgn-tools.

from __future__ import annotations

from typing import List

from sos.vmcore_report.emitters.registry import emits


def _get_active_numa_nodes(prog):
    nodes = []
    try:
        node_data = prog["node_data"]
        max_nodes = len(prog["preferred_node_policy"])
        for i in range(max_nodes):
            try:
                if node_data[i].value_() != 0:
                    nodes.append(node_data[i])
            except Exception:
                continue
    except Exception:
        pass
    return nodes


def _for_each_node_zone(prog, node):
    try:
        node_zones = node.node_zones
        nr = int(node.nr_zones.value_())
        for j in range(nr):
            yield node_zones[j]
    except Exception:
        return


def _get_per_zone_buddyinfo(zone) -> List[int]:
    free_area = zone.free_area.read_()
    return [int(x.nr_free.value_()) for x in free_area]


@emits("proc/buddyinfo")
def emit_proc_buddyinfo(prog) -> str:
    # /proc/buddyinfo format example:
    # Node 0, zone      DMA      1      2 ...
    # We will use: "Node N, zone <name>" then orders.
    lines: List[str] = []

    active_nodes = _get_active_numa_nodes(prog)
    for node_id, node in enumerate(active_nodes):
        for zone in _for_each_node_zone(prog, node) or ():
            try:
                zone_name = zone.name.string_().decode("utf-8")
            except Exception:
                zone_name = "unknown"
            try:
                blocks = _get_per_zone_buddyinfo(zone)
            except Exception:
                blocks = []
            if not blocks:
                continue
            # match proc spacing roughly
            prefix = f"Node {node_id}, zone {zone_name:>8} "
            orders = " ".join(f"{b:7d}" for b in blocks)
            lines.append(prefix + orders + "\n")

    if not lines:
        return "# vmcore-report: stub (/proc/buddyinfo not reconstructable)\n"
    return "".join(lines)
