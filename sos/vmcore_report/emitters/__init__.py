# Namespace package for vmcore-report emitters (proc/, sys/, sos_commands/).
# Emitters should be organized by domain subpackages:
#   - sos.vmcore_report.emitters.proc
#   - sos.vmcore_report.emitters.sys
#   - sos.vmcore_report.emitters.commands
#
# Each emitter module registers producers using:
#   from sos.vmcore_report.emitters.registry import emits, emits_many
#
# Producers must return text (str). Wrapper plugins handle discovery and writing.
