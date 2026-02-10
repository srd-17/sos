# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/mounts reconstructed from vmcore via drgn.
#
# Adapted from drgn-tools drgn_tools/mounts.py (no runtime drgn-tools dep).
#
# Note: /proc/mounts normally has 6 columns:
#   <src> <target> <fstype> <options> <dump> <pass>
# We can reconstruct src/target/fstype/options best-effort. dump/pass are set to 0.

from __future__ import annotations

from typing import List

import drgn.helpers.linux.fs

from sos.vmcore_report.emitters.registry import emits


@emits("proc/mounts")
def emit_proc_mounts(prog) -> str:
    lines: List[str] = []
    try:
        mnt_ns = prog["init_task"].nsproxy.mnt_ns
        for mnt in drgn.helpers.linux.fs.for_each_mount(mnt_ns):
            try:
                devname = mnt.mnt_devname.string_().decode("utf-8", errors="replace")
            except Exception:
                devname = "unknown"

            try:
                sb = mnt.mnt.mnt_sb
                fstype = sb.s_type.name.string_()
                if sb.s_subtype:
                    fstype += b"." + sb.s_subtype.string_()
                fstype_s = fstype.decode("utf-8", errors="replace")
            except Exception:
                fstype_s = "unknown"

            try:
                target = drgn.helpers.linux.fs.d_path(
                    mnt.mnt_parent.mnt.address_of_(), mnt.mnt_mountpoint
                ).decode("utf-8", errors="replace")
            except Exception:
                target = "/"

            # mount options
            options = "rw"
            try:
                if hasattr(mnt.mnt, "mnt_flags"):
                    mnt_flags = int(mnt.mnt.mnt_flags.value_())
                    # very small subset: MS_RDONLY
                    ms_rdonly = int(prog.constant("MS_RDONLY"))
                    if mnt_flags & ms_rdonly:
                        options = "ro"
            except Exception:
                pass

            lines.append(f"{devname} {target} {fstype_s} {options} 0 0\n")

        if not lines:
            return "# vmcore-report: stub (/proc/mounts not reconstructable)\n"
        return "".join(lines)
    except Exception as e:
        return f"# vmcore-report: stub (/proc/mounts not reconstructable) - {e}\n"
