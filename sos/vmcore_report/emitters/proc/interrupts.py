# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/interrupts reconstructed from vmcore via drgn.
#
# drgn-tools irq.py provides rich IRQ descriptor iteration, but not the exact
# /proc/interrupts text format. This emitter uses the same underlying sources:
# - for_each_present_cpu()
# - irq descriptor lookup (sparse_irqs maple tree / irq_desc_tree radix tree)
# - per-cpu desc.kstat_irqs counts
# - desc.action.name for the label when present

from __future__ import annotations

from typing import List, Optional, Tuple

from drgn import NULL, Object
from drgn.helpers.common.format import escape_ascii_string
from drgn.helpers.linux.cpumask import for_each_present_cpu
from drgn.helpers.linux.mapletree import mtree_load
from drgn.helpers.linux.percpu import per_cpu_ptr
from drgn.helpers.linux.radixtree import radix_tree_lookup

from sos.vmcore_report.emitters.registry import emits


def _sparse_irq_supported(prog) -> Tuple[bool, str]:
    try:
        _ = prog["sparse_irqs"]
        return True, "maple"
    except Exception:
        try:
            _ = prog["irq_desc_tree"]
            return True, "radix"
        except Exception:
            return False, ""


def _irq_to_desc(prog, irq: int) -> Object:
    supported, tree_type = _sparse_irq_supported(prog)
    if supported:
        if tree_type == "radix":
            addr = radix_tree_lookup(prog["irq_desc_tree"].address_of_(), irq)
        else:
            addr = mtree_load(prog["sparse_irqs"].address_of_(), irq)

        if addr:
            return Object(prog, "struct irq_desc", address=addr).address_of_()
        return NULL(prog, "void *")
    # legacy flat array
    try:
        return (prog["irq_desc"][irq]).address_of_()
    except Exception:
        return NULL(prog, "void *")


def _kstat_irqs_cpu(prog, desc: Object, cpu: int) -> int:
    try:
        addr = per_cpu_ptr(desc.kstat_irqs, cpu)
        return int(Object(prog, "int", address=addr).value_())
    except Exception:
        return 0


def _iter_in_use_irqs(prog):
    try:
        nr_irqs = int(prog["nr_irqs"].value_())
    except Exception:
        nr_irqs = 0
    for irq in range(nr_irqs):
        desc = _irq_to_desc(prog, irq)
        if desc and int(desc.value_()) != 0:
            yield irq, desc


def _desc_name(desc: Object) -> str:
    try:
        if desc.action:
            return escape_ascii_string(desc.action.name.string_(), escape_backslash=True)
    except Exception:
        pass
    return ""


@emits("proc/interrupts")
def emit_proc_interrupts(prog) -> str:
    try:
        cpus = [int(c) for c in for_each_present_cpu(prog)]
        # Header (CPU columns)
        hdr = "           " + " ".join(f"CPU{c:>7d}" for c in cpus) + "\n"

        lines: List[str] = [hdr]
        for irq, desc in _iter_in_use_irqs(prog):
            counts = [str(_kstat_irqs_cpu(prog, desc, c)).rjust(10) for c in cpus]
            name = _desc_name(desc)

            # Best-effort "type" column (edge/level/etc) if present
            itype: Optional[str] = None
            try:
                if getattr(desc, "irq_data", None) and getattr(desc.irq_data, "chip", None):
                    chip = desc.irq_data.chip
                    if chip and getattr(chip, "name", None):
                        itype = chip.name.string_().decode("utf-8", errors="replace")
            except Exception:
                itype = None

            # Format: "<irq>: <counts...> <type> <name>"
            # Keep it stable even if type/name missing.
            tail = ""
            if itype:
                tail += f" {itype}"
            if name:
                tail += f" {name}"

            lines.append(f"{irq:>5d}: {''.join(counts)}{tail}\n")

        if len(lines) == 1:
            return "# vmcore-report: stub (/proc/interrupts not reconstructable)\n"
        return "".join(lines)
    except Exception as e:
        return f"# vmcore-report: stub (/proc/interrupts not reconstructable) - {e}\n"
