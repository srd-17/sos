# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/cpuinfo (best-effort) as a single file with one stanza per CPU.
#
# We reconstruct a minimal subset of commonly parsed fields:
#   processor, architecture, model name (best-effort), vendor_id (best-effort)
# Unknown values are filled with "unknown" and a header indicates partial data.

from sos.vmcore_report.emitters.registry import emits, enumerate_cpus


def _uts_machine(prog) -> str:
    try:
        name = prog["init_uts_ns"].name
        s = name.machine.string_()
        return s.decode("utf-8", "ignore").strip() if isinstance(s, (bytes, bytearray)) else str(s).strip()
    except Exception:
        return ""


def _vendor_id(prog) -> str:
    # Best-effort placeholder; true vendor id is not trivial from vmcore.
    # Keep a stable token so downstream tooling can distinguish.
    return "unknown"


def _model_name(prog) -> str:
    # Best-effort placeholder; per-CPU brand strings are not trivial from vmcore.
    return "unknown"


@emits("proc/cpuinfo")
def emit_proc_cpuinfo(prog) -> str:
    arch = _uts_machine(prog) or "unknown"
    lines = []
    warned = False
    try:
        cpus = list(enumerate_cpus(prog))
    except Exception:
        cpus = [{"cpu": 0}]
        warned = True

    if warned or not arch or any(_v == "unknown" for _v in (_vendor_id(prog), _model_name(prog))):
        lines.append("# vmcore-report: partial /proc/cpuinfo (best-effort)\n")

    for item in cpus:
        cpu = int(item.get("cpu", 0))
        lines.append(f"processor\t: {cpu}\n")
        lines.append(f"architecture\t: {arch}\n")
        lines.append(f"vendor_id\t: {_vendor_id(prog)}\n")
        lines.append(f"model name\t: {_model_name(prog)}\n")
        # Blank line between stanzas
        lines.append("\n")

    return "".join(lines)
