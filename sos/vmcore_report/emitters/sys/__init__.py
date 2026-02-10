# Namespace for sys/ emitters (vmcore-report).
# Emitters in this package should use the decorators from
# sos.vmcore_report.emitters.registry:
#
#   from sos.vmcore_report.emitters.registry import emits, emits_many
#
# and implement small producers that return text (str). Any symbol
# lookups should be defensive; return concise text and let the
# registry/wrapper handle stubbing/logging on failures.
