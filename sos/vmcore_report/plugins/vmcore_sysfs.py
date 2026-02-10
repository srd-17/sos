# This file is part of the sos project: https://github.com/sosreport/sos
#
# Wrapper plugin that generates sys/ replicas via emitter discovery.
#
# Two-layer model:
# - Drivers (this plugin) orchestrate sys emitters and write sys/* paths.
# - Emitters own all sys/* content. No standalone plugin should write sys/*.

from sos.vmcore_report.plugin import DrgnPluginBase
from sos.vmcore_report.emitters.registry import run_scope
from sos.vmcore_report.emitters.writer import write_files


class VmcoreSysfsPlugin(DrgnPluginBase):
    plugin_name = "vmcore_sysfs"
    description = "Reconstruct sys/ tree from vmcore (best-effort)"
    default_enabled = True
    experimental = False

    def collect(self, prog):
        # Discover and run all sys/ emitters, then write outputs directly
        # into the archive under sys/... (exact paths).
        try:
            outputs = run_scope("sys", prog, logger=self.logger)
        except Exception as e:
            # Preserve at least a stub root to signal failure in archive
            self.write_text("ERROR.txt", f"sys emitters failed: {e}\n")
            return
        write_files(self.archive, outputs)
