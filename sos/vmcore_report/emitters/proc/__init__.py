# Namespace for proc/ emitters (vmcore-report).
# Use decorators from sos.vmcore_report.emitters.registry:
#
#   from sos.vmcore_report.emitters.registry import emits, emits_many
#
# Emitters should:
# - Return text (str) only; no direct archive writes
# - Be defensive with symbol access; degrade to concise WARN text if needed
# - Keep formatting consistent with proc file expectations where practical
