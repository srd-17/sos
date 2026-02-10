# sos vmcore-report — Ultra-brief

What
- Offline sos subcommand. Reads vmcore with drgn. Writes sos-style archive (proc/, sys/, sos_commands/, sos_logs/, sos_reports/). No drgn-tools.

Run
- Basic:
  python3 bin/sos vmcore-report --vmcore /path/to/vmcore --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux
- Extra debuginfo:
  python3 bin/sos vmcore-report --vmcore /path/to/vmcore --debuginfo-dir /usr/lib/debug

How
- drgn.Program() → set_core_dump(vmcore) → load debuginfo (explicit or default/main).
- Wrapper plugins auto-run: vmcore_procfs (proc/), vmcore_sysfs (sys/), vmcore_commands (sos_commands/).
- Emitters return text for exact paths; if unknown, write “# vmcore-report: stub/best-effort …”.

Add a drgn plugin (logic)
- File: sos/vmcore_report/plugins/my_plugin.py

from sos.vmcore_report.plugin import DrgnPluginBase

class MyPlugin(DrgnPluginBase):
    plugin_name = "my_plugin"
    def collect(self, prog):
        self.write_text("summary.txt", "ok\n")

Add an emitter (replicate a file)
- Fixed path (proc/modules):

from sos.vmcore_report.emitters.registry import emits

@emits("proc/modules")
def emit_proc_modules(prog) -> str:
    return "kernel 0 0\n"

- Fixed path (sys/devices/system/cpu/online):

from sos.vmcore_report.emitters.registry import emits

@emits("sys/devices/system/cpu/online")
def emit_online(prog) -> str:
    return "0-1\n"

Quick check
- tar -tf /var/tmp/sosvmcore-*.tar.* | sort | head
- tar -xOf /var/tmp/sosvmcore-*.tar.* sosvmcore-*/proc/modules | head
