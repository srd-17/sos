# This file is part of the sos project: https://github.com/sosreport/sos
#
# sos_commands/kernel emitters:
#  - uname_-a           (best-effort from utsname)
#  - lsmod              (from Program.modules(); columns: Module Size Used by)

from sos.vmcore_report.emitters.registry import emits


@emits("sos_commands/kernel/uname_-a")
def emit_uname_a(prog) -> str:
    # Try to reconstruct a uname -a style line:
    # sysname nodename release version machine
    sysname = nodename = release = version = machine = ""
    try:
        name = prog["init_uts_ns"].name
        def txt(x):
            try:
                s = x.string_()
                return s.decode("utf-8", "ignore") if isinstance(s, (bytes, bytearray)) else str(s)
            except Exception:
                return ""
        sysname = txt(name.sysname).strip()
        nodename = txt(name.nodename).strip()
        release = txt(name.release).strip()
        version = txt(name.version).strip()
        machine = txt(name.machine).strip()
    except Exception:
        # Best-effort via linux_banner (optional)
        try:
            import drgn as _drgn
            b = _drgn.cast("char *", prog["linux_banner"]).string_()
            banner = b.decode("utf-8", "ignore") if isinstance(b, (bytes, bytearray)) else str(b)
            # Use banner as-version fallback
            version = banner.strip()
        except Exception:
            pass

    parts = [p for p in (sysname, nodename, release, version, machine) if p]
    if not parts:
        return "# vmcore-report: stub (uname -a not reconstructable from vmcore)\n"
    line = " ".join(parts)
    return f"{line}\n"


@emits("sos_commands/kernel/lsmod")
def emit_lsmod(prog) -> str:
    # Produce a minimal lsmod-like table from Program.modules()
    # Columns: Module Size Used by
    header = "Module                  Size  Used by\n"
    lines = []
    try:
        mods = getattr(prog, "modules", None)
        if callable(mods):
            modules = list(mods())
        else:
            # Older drgn may expose iterable at prog.modules
            modules = list(mods) if mods is not None else []
    except Exception:
        modules = []

    if not modules:
        return "# vmcore-report: stub (no modules info available)\n"

    def mod_name(m):
        for attr in ("name",):
            try:
                v = getattr(m, attr)
                return v.decode("utf-8", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
            except Exception:
                continue
        return "unknown"

    def mod_size(m):
        for attr in ("size",):
            try:
                return int(getattr(m, attr))
            except Exception:
                continue
        return 0

    def mod_refcnt(m):
        for attr in ("refcnt", "refcnt", "ref_count"):
            try:
                return int(getattr(m, attr))
            except Exception:
                continue
        # Some drgn module objects may provide "refcnt" through methods/fields differently
        return 0

    # Used by list is generally not trivial to reconstruct; leave empty
    for m in modules:
        name = mod_name(m)
        size = mod_size(m)
        used = mod_refcnt(m)
        lines.append(f"{name:<22} {size:>8}  {used}\n")

    return header + "".join(lines)
