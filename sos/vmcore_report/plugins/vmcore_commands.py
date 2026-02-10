# This file is part of the sos project: https://github.com/sosreport/sos
#
# Wrapper plugin that generates sos_commands/ outputs via emitter discovery.

from sos.vmcore_report.plugin import DrgnPluginBase
from sos.vmcore_report.emitters.registry import run_scope
from sos.vmcore_report.emitters.writer import write_files


class VmcoreCommandsPlugin(DrgnPluginBase):
    plugin_name = "vmcore_commands"
    description = "Reconstruct sos_commands/ outputs from vmcore (best-effort)"
    default_enabled = True
    experimental = False

    def collect(self, prog):
        # Discover and run all sos_commands emitters; write into sos_commands/...
        try:
            outputs = run_scope("commands", prog, logger=self.logger)
        except Exception as e:
            self.write_text("ERROR.txt", f"sos_commands emitters failed: {e}\n")
            return
        write_files(self.archive, outputs)
