# This file is part of the sos project: https://github.com/sosreport/sos
#
# Wrapper plugin that generates proc/ replicas via emitter discovery.

from sos.vmcore_report.plugin import DrgnPluginBase
from sos.vmcore_report.emitters.registry import run_scope
from sos.vmcore_report.emitters.writer import write_files


class VmcoreProcfsPlugin(DrgnPluginBase):
    plugin_name = "vmcore_procfs"
    description = "Reconstruct proc/ tree from vmcore (best-effort)"
    default_enabled = True
    experimental = False

    def collect(self, prog):
        # Discover and run all proc/ emitters, then write outputs directly
        # into the archive under proc/... (exact paths).
        try:
            outputs = run_scope("proc", prog, logger=self.logger)
        except Exception as e:
            # Preserve at least a stub root to signal failure in archive
            self.write_text("ERROR.txt", f"proc emitters failed: {e}\n")
            return
        write_files(self.archive, outputs)
