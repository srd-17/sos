# Namespace for sos_commands/ emitters (vmcore-report).
# Use decorators from sos.vmcore_report.emitters.registry:
#
#   from sos.vmcore_report.emitters.registry import emits, emits_many
#
# Emitters should return text (str) only; wrapper plugins handle writing
# into sos_commands/... paths in the archive.
