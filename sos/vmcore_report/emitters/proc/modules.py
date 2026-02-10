# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/modules (best-effort) from drgn Program.modules().
#
# /proc/modules format (typical):
#   <module> <size> <refcount> <deps> <state> <address>
#
# We reconstruct a minimal, compatible subset:
#   <module> <size> <refcount>
# Dependencies, state, and address are omitted (not reliably derivable from vmcore).
# If modules cannot be enumerated, emit a clear stub with a WARN header.

from sos.vmcore_report.emitters.registry import emits


@emits("proc/modules")
def emit_proc_modules(prog) -> str:
    lines = []
    try:
        mods_attr = getattr(prog, "modules", None)
        if callable(mods_attr):
            modules = list(mods_attr())
        else:
            modules = list(mods_attr) if mods_attr is not None else []
    except Exception:
        modules = []

    if not modules:
        return "# vmcore-report: stub (/proc/modules not reconstructable from vmcore)\n"

    def mod_name(m):
        try:
            v = getattr(m, "name")
            return v.decode("utf-8", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
        except Exception:
            return "unknown"

    def mod_size(m):
        try:
            return int(getattr(m, "size"))
        except Exception:
            return 0

    def mod_refcnt(m):
        for attr in ("refcnt", "ref_count", "refcnts", "usecount"):
            try:
                v = getattr(m, attr)
                return int(v)
            except Exception:
                continue
        return 0

    for m in modules:
        name = mod_name(m)
        size = mod_size(m)
        refc = mod_refcnt(m)
        # Minimal fields; keep spaces to be grep-friendly
        lines.append(f"{name} {size} {refc}\n")

    return "".join(lines)
