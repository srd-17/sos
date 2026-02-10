# sos vmcore-report — Quick Start (Very Brief)

What it does
- Reads a kernel vmcore with drgn and produces a sos-style archive (sos_logs, sos_reports, proc/, sys/, sos_commands/, and plugin outputs).
- No live system access, no drgn-tools dependency. drgn-only loader with best-effort debuginfo (prefer --vmlinux/--debuginfo-dir).

How it works (pipeline)
- Loader: drgn.Program(); set_core_dump(vmcore) (+ sudohelper fallback) → load debuginfo (explicit vmlinux/+*.ko.debug, else default/main).
- Orchestration (wrapper plugins, auto-enabled):
  - vmcore_procfs → runs proc/ emitters
  - vmcore_sysfs → runs sys/ emitters
  - vmcore_commands → runs sos_commands/ emitters
  - kernel_info, sched_debug (drgn-backed plugins)
- Emitters:
  - Tiny producers returning text for exact archive paths under proc/, sys/, sos_commands/.
  - Discovered dynamically via decorators (no central map edits).
  - Best-effort with clear WARN headers when data can’t be reconstructed.

CLI usage
- Basic:
  python3 bin/sos vmcore-report --vmcore /path/to/vmcore --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux
- Add extra debuginfo dirs:
  python3 bin/sos vmcore-report --vmcore /path/to/vmcore --debuginfo-dir /usr/lib/debug --debuginfo-dir /opt/debug
- Selection (like sos report):
  --only-plugins vmcore_procfs,vmcore_sysfs,vmcore_commands
  --skip-plugins kernel_info

Archive structure (subset)
- sos_reports/manifest.json
- sos_logs/{sos.log, ui.log}
- proc/{cpuinfo, modules, ...}            ← from emitters
- sys/devices/system/cpu/{online,present,possible, ...}  ← from emitters
- sos_commands/kernel/{uname_-a, lsmod, ...} ← from emitters
- plugins/{kernel_info, sched_debug}/...   ← drgn plugins

Add a new drgn plugin (logic analyzer)
- Location: sos/vmcore_report/plugins/my_plugin.py
- Derive from DrgnPluginBase and write to your own directory under plugins/my_plugin (or optionally to a canonical path via archive.add_string):

from sos.vmcore_report.plugin import DrgnPluginBase

class MyPlugin(DrgnPluginBase):
    plugin_name = "my_plugin"
    description = "Short description"
    default_enabled = True

    def check_enabled(self, prog):
        try:
            _ = prog["some_symbol"]
            return True
        except Exception:
            return False

    def collect(self, prog):
        # Best-effort: catch exceptions and keep going
        try:
            # do drgn lookups, build text/json, etc.
            self.write_text("summary.txt", "collection ok\n")
        except Exception as e:
            self.write_text("ERROR.txt", f"{e}\n")

Add a new emitter (replicate a proc/sys/sos_commands file)
- Use decorators from the emitter registry. Emitters return text; wrapper plugins write them at exact archive paths.

1) Fixed path (e.g., proc/cpuinfo):

from sos.vmcore_report.emitters.registry import emits

@emits("proc/cpuinfo")
def emit_proc_cpuinfo(prog) -> str:
    return "# vmcore-report: partial /proc/cpuinfo (best-effort)\nprocessor\t: 0\n\n"

2) Another fixed path (e.g., sys/devices/system/cpu/online):

from sos.vmcore_report.emitters.registry import emits

@emits("sys/devices/system/cpu/online")
def emit_online(prog) -> str:
    return "0-3\n"  # best-effort; use WARN header if approximated

3) Templated path (per-pid), if needed:

from sos.vmcore_report.emitters.registry import emits_many

@emits_many("proc/{pid}/status", enumerator="enumerate_pids")
def emit_proc_pid_status(prog, pid: int) -> str:
    return f"Name:\t(pid {pid})\nState:\tunknown\n"

Testing quickly
- Run:
  python3 bin/sos vmcore-report --vmcore /path/to/vmcore --only-plugins vmcore_procfs,vmcore_sysfs,vmcore_commands
- Inspect:
  tar -tf /var/tmp/sosvmcore-*.tar.* | sort | head -n 200
  tar -xOf /var/tmp/sosvmcore-*.tar.* sosvmcore-*/proc/modules | head

Contributor tips (keep PRs independent)
- Put emitters in:
  - sos/vmcore_report/emitters/proc/*.py
  - sos/vmcore_report/emitters/sys/*.py
  - sos/vmcore_report/emitters/commands/*.py
- No central registry edits; discovery is automatic.
- Keep emitters small, defensive, and best-effort with WARN headers if incomplete.

Notes
- When data isn’t reconstructable from vmcore, emit the file with a short “# vmcore-report: stub/best-effort …” line rather than omit it. This preserves shape parity with sos report.
