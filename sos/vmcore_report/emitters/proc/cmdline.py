# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/cmdline from vmcore via drgn.
#
# Adapted from drgn-tools drgn_tools/cmdline.py (no runtime drgn-tools dep).

from __future__ import annotations

from sos.vmcore_report.emitters.registry import emits


@emits("proc/cmdline")
def emit_proc_cmdline(prog) -> str:
    try:
        cmdline = prog["saved_command_line"].string_().decode("utf-8", errors="replace")
        # /proc/cmdline ends with a newline
        return cmdline.rstrip("\n") + "\n"
    except Exception as e:
        return f"# vmcore-report: stub (/proc/cmdline not reconstructable) - {e}\n"
