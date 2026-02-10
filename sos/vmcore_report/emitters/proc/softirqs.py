# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/softirqs reconstructed from vmcore via drgn.
#
# drgn-tools irq.py doesn't implement softirq rendering. We reconstruct it using
# the standard kernel symbols:
# - softirq_vec[]: names of softirq types (or fallback to enum)
# - __per_cpu__kstat.softirqs (or similar) for per-cpu counts
#
# This is best-effort across kernel versions.

from __future__ import annotations

from typing import List

from drgn.helpers.linux.cpumask import for_each_present_cpu
from drgn.helpers.linux.percpu import per_cpu_ptr

from sos.vmcore_report.emitters.registry import emits


def _softirq_names(prog) -> List[str]:
    # Preferred: softirq_vec[].action or .name depending on kernel
    names: List[str] = []
    try:
        vec = prog["softirq_vec"].read_()
        for item in vec:
            nm = None
            try:
                if hasattr(item, "action") and item.action:
                    # action is function pointer; no name
                    nm = None
            except Exception:
                pass
            try:
                if hasattr(item, "name") and item.name:
                    nm = item.name.string_().decode("utf-8", errors="replace")
            except Exception:
                pass
            if nm is None:
                names.append("unknown")
            else:
                names.append(nm)
        if any(n != "unknown" for n in names):
            return names
    except Exception:
        pass

    # Fallback: enum softirq_nr if available
    try:
        enum_obj = prog.type("enum")
        _ = enum_obj  # unused
    except Exception:
        pass

    # Standard Linux softirq names in order
    return [
        "HI",
        "TIMER",
        "NET_TX",
        "NET_RX",
        "BLOCK",
        "IRQ_POLL",
        "TASKLET",
        "SCHED",
        "HRTIMER",
        "RCU",
    ]


def _get_percpu_softirqs(prog, cpu: int, nsoft: int) -> List[int]:
    # Many kernels: __per_cpu__kstat is a percpu struct kernel_stat and has .softirqs[]
    try:
        kstat = prog["kstat"]
        ks = per_cpu_ptr(kstat, cpu)
        try:
            arr = ks.softirqs
            return [int(arr[i].value_()) for i in range(nsoft)]
        except Exception:
            pass
    except Exception:
        pass

    # Older style: kstat_softirqs is percpu array
    try:
        arr = prog["kstat_softirqs"]
        base = per_cpu_ptr(arr, cpu)
        # base points to first element
        return [int(base[i].value_()) for i in range(nsoft)]
    except Exception:
        pass

    return [0] * nsoft


@emits("proc/softirqs")
def emit_proc_softirqs(prog) -> str:
    try:
        cpus = [int(c) for c in for_each_present_cpu(prog)]
        names = _softirq_names(prog)
        nsoft = len(names)

        out: List[str] = []
        hdr = "                    " + " ".join(f"CPU{c:>7d}" for c in cpus) + "\n"
        out.append(hdr)

        for idx, name in enumerate(names):
            counts = []
            for cpu in cpus:
                vals = _get_percpu_softirqs(prog, cpu, nsoft)
                counts.append(str(vals[idx]).rjust(10))
            out.append(f"{name + ':': <16}{''.join(counts)}\n")

        return "".join(out)
    except Exception as e:
        return f"# vmcore-report: stub (/proc/softirqs not reconstructable) - {e}\n"
