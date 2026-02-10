# sos vmcore-report — Comprehensive Guide

Overview
- Purpose: Analyze an offline kernel crash dump (vmcore) using drgn and produce a sos-style archive that mirrors sos report structure without touching a live system.
- Dependencies: Python + drgn (no drgn-tools). Best-effort debuginfo loading from provided vmlinux and optional --debuginfo-dir locations.
- Output: sos_logs/, sos_reports/ (manifest), and replicas of key trees proc/, sys/, sos_commands/, plus drgn plugin outputs (e.g., kernel_info, sched_debug).

When to use
- You have a vmcore and (ideally) a matching vmlinux (uncompressed image with symbols) and want a sos-like artifact to inspect and/or feed to downstream tools.

Key differences from sos report
- sos report collects from a running system via add_cmd_output()/add_copy_spec().
- vmcore-report reconstructs equivalent files from a vmcore via drgn (“virtual proc/sys/commands”).
- Root not required. No system changes.

Usage
- Basic run:
  python3 bin/sos vmcore-report --vmcore /path/to/vmcore --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux
- Add extra debuginfo roots (search for vmlinux, *.ko.debug):
  python3 bin/sos vmcore-report --vmcore /vmcore --vmlinux /usr/lib/debug/.../vmlinux --debuginfo-dir /usr/lib/debug --debuginfo-dir /opt/debug
- Selection controls:
  --only-plugins vmcore_procfs,vmcore_sysfs,vmcore_commands
  --skip-plugins kernel_info
  --enable-plugins my_plugin

Execution pipeline (end-to-end)
1) Loader (drgn-only)
   - Create Program(): prog = drgn.Program()
   - Set core dump: prog.set_core_dump(vmcore)
     - Fallback: drgn.internal.sudohelper.open_via_sudo on PermissionError
   - Debuginfo loading (best-effort):
     - If --vmlinux given, load it; also recursively add *.ko.debug under each --debuginfo-dir
     - Else attempt default mechanisms:
       - prog.load_default_debug_info() if present
       - Else prog.load_debug_info(default=True) or prog.load_debug_info(main=True) based on drgn version
   - Failure to load debuginfo is non-fatal; emitters/plugins are defensive and will annotate missing values.

2) Orchestration via wrapper plugins (enabled by default)
   - vmcore_procfs: discovers and runs proc/ emitters; writes outputs under proc/...
   - vmcore_sysfs: discovers and runs sys/ emitters; writes outputs under sys/...
   - vmcore_commands: discovers and runs sos_commands/ emitters; writes outputs under sos_commands/...
   - kernel_info: collects utsname, banner, taints via drgn

Strict organization note (two-layer model)
- Drivers (vmcore_procfs/vmcore_sysfs/vmcore_commands) orchestrate emitters and write files into the archive.
- Emitters (emitters/proc, emitters/sys, emitters/commands) own all proc/*, sys/*, and sos_commands/* content.
- Example: sys/kernel/debug/sched/debug is produced by emitters/sys/sched_debug.py (generator + @emits in the same module).
- No standalone plugin should write proc/* or sys/* in vmcore-report mode.

3) Emitters (virtual proc/sys/sos_commands)
   - Tiny, pure functions that return text (no archive IO)
   - Decorated with @emits(path) or @emits_many(template, enumerator)
   - Discovered dynamically (no central registry edits)
   - If a value is not reconstructable, return a brief “# vmcore-report: stub/best-effort …” header and any partial info

4) Archive finalization
   - sos_reports/manifest.json (policy-managed)
   - sos_logs/{sos.log, ui.log}
   - Compression, checksum generation, and standard sos output message

Architecture
- sos/vmcore_report/drgn_context.py: drgn-only loader
- sos/vmcore_report/plugins/: drgn plugins (logic analyzers)
  - vmcore_procfs.py / vmcore_sysfs.py / vmcore_commands.py orchestrate emitters
  - kernel_info.py, sched_debug.py provide exemplar drgn-based collectors
- sos/vmcore_report/emitters/: virtual fs “producers”
  - registry.py: decorator API and discovery (auto-imports emitters under emitters/{proc,sys,commands})
  - writer.py: write_file()/write_files() helpers (wrapper plugins call these)
  - proc/*: emitters for proc paths (cpuinfo, modules, meminfo, vmstat, …)
  - sys/*: emitters for sys paths (CPU masks, topology, …)
  - commands/*: emitters for sos_commands “command outputs” (uname_-a, lsmod, …)

Emitter registry (discovery and API)
- Register a fixed-path emitter:
  from sos.vmcore_report.emitters.registry import emits

  @emits("proc/modules")
  def emit_proc_modules(prog) -> str:
      return "kernel 0 0\n"  # best-effort; real implementation may inspect Program.modules()

- Register a templated emitter (per-pid, per-cpu, etc.):
  from sos.vmcore_report.emitters.registry import emits_many

  @emits_many("proc/{pid}/status", enumerator="enumerate_pids")
  def emit_proc_pid_status(prog, pid: int) -> str:
      return f"Name:\t(pid {pid})\nState:\tunknown\n"

- Enumerators provided:
  - enumerate_cpus(prog) → yields {'cpu': n} for each cpu (best-effort: online→present→possible→nr_cpu_ids→0)
  - enumerate_pids(prog) → yields {'pid': pid} for each task (drgn helpers or init_task traversal as fallback)

- Writer (used by wrapper plugins):
  - write_file(archive, "proc/cpuinfo", "...text...")
  - write_files(archive, [(path, text), ...])

Drgn plugin model (logic analyzers)
- Derive from DrgnPluginBase, implement collect(self, prog)
- Write outputs under plugins/<name>/ (JSON/text), or deliberately to canonical paths using archive.add_string()

Example: minimal logic plugin
from sos.vmcore_report.plugin import DrgnPluginBase

class MyPlugin(DrgnPluginBase):
    plugin_name = "my_plugin"
    description = "Example drgn analysis"
    default_enabled = True

    def check_enabled(self, prog):
        try:
            _ = prog["init_task"]
            return True
        except Exception:
            return False

    def collect(self, prog):
        # Best-effort; catch exceptions
        try:
            self.write_text("summary.txt", "ok\n")
        except Exception as e:
            self.write_text("ERROR.txt", f"{e}\n")

Adding a new emitter (replicating a file)
- Fixed path (e.g., sys/devices/system/cpu/online)
from sos.vmcore_report.emitters.registry import emits

@emits("sys/devices/system/cpu/online")
def emit_online(prog) -> str:
    # Example: if cpumasks available, return “0-3”; else annotate
    return "# vmcore-report: approximate\n0-1\n"

- Templated path (per-pid status)
from sos.vmcore_report.emitters.registry import emits_many

@emits_many("proc/{pid}/status", enumerator="enumerate_pids")
def emit_pid_status(prog, pid: int) -> str:
    # Minimal skeleton; fill in when fields are derivable
    return f"Name:\tpid{pid}\nState:\tunknown\n"

- Command-style output under sos_commands
from sos.vmcore_report.emitters.registry import emits

@emits("sos_commands/kernel/uname_-a")
def emit_uname(prog) -> str:
    try:
        nm = prog["init_uts_ns"].name
        def S(x): return x.decode("utf-8","ignore") if isinstance(x,(bytes,bytearray)) else str(x)
        return f"{S(nm.sysname.string_())} {S(nm.nodename.string_())} {S(nm.release.string_())} {S(nm.version.string_())} {S(nm.machine.string_())}\n"
    except Exception:
        return "# vmcore-report: stub (uname not reconstructable)\n"

Content quality policy (best-effort)
- Always preserve expected path presence for parity (do not omit files)
- When not reconstructable, return a short “# vmcore-report: stub/best-effort …” header and any hints you can compute
- Keep formatting faithful enough for tools (headers/columns or key:value lines)

Examples already implemented (subset)
- sys/devices/system/cpu/{online,present,possible}: mask ranges (approximate if cpumask symbols absent)
- proc/modules: module name/size/refcount from Program.modules() where available
- proc/cpuinfo: per-CPU stanzas with architecture (utsname), placeholders where unknown
- sos_commands/kernel/uname_-a: from utsname/banner
- sos_commands/kernel/lsmod: lsmod-like table from Program.modules()

Testing
1) Generate archive:
   python3 bin/sos vmcore-report --vmcore /path/to/vmcore --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux
2) Inspect:
   tar -tf /var/tmp/sosvmcore-*.tar.* | sort | head -n 200
   tar -xOf /var/tmp/sosvmcore-*.tar.* sosvmcore-*/proc/modules | head -n 20
   tar -xOf /var/tmp/sosvmcore-*.tar.* sosvmcore-*/sys/devices/system/cpu/online
   tar -xOf /var/tmp/sosvmcore-*.tar.* sosvmcore-*/sos_commands/kernel/uname_-a
3) Logs:
   - sos_logs/ui.log: user-facing progress
   - sos_logs/sos.log: detailed errors/warnings (including drgn failures)

Unit testing ideas (optional)
- Mock drgn.Program with attributes used by your emitter (e.g., init_uts_ns or modules())
- Assert the exact text shape (keys, columns) against small golden strings
- Ensure emitters handle missing symbols by returning stub headers (no exceptions)

Contributor workflow (parallel-friendly)
- File placement:
  - Emitters: sos/vmcore_report/emitters/{proc,sys,commands}/your_feature.py
  - Drgn plugins: sos/vmcore_report/plugins/your_plugin.py
- No central registry edits: decorators + package discovery auto-register your emitters
- Keep PRs small and domain-scoped (one emitter or closely-related set)
- Be defensive; document kernel-version caveats in module docstrings

Design principles (why this approach)
- Parity first: Recreate archive shape so downstream tooling recognizes expected paths
- Best-effort semantics: Offline dumps vary; emit partial info with clear headers instead of failing
- Separation of concerns: Emitters (content) vs. wrapper plugins (orchestration) vs. loader (drgn)
- Extensibility: New emitters are trivial to add; discovery avoids merge conflicts
- Upstream-friendly: drgn-only, isolated code; existing sos report unaffected

Limitations
- Some userspace-only data (e.g., systemd state) cannot be reconstructed from vmcore; emit stubs
- Detailed per-pid trees may be large; defaults target full parity, but can be gated by future flags
- Debuginfo availability governs fidelity; provide --vmlinux and --debuginfo-dir for best results

Roadmap (suggested)
- sys topology files: cpuN/topology/* (core_id, package id, sibling lists)
- Memory: proc/meminfo, proc/vmstat, proc/zoneinfo, proc/slabinfo
- Per-PID expansions: status, limits, cgroup, cpuset, mountinfo, oom_*, stack
- Coverage reports: JSON summary of emitted vs expected proc/sys/sos_commands paths

Appendix: quick snippets

- Minimal drgn plugin writing to canonical path (not plugins/):
from sos.vmcore_report.plugin import DrgnPluginBase

class WriteToSys(DrgnPluginBase):
    plugin_name = "write_to_sys"
    def collect(self, prog):
        self.archive.add_string("hello\n", "sys/say/hello", mode="w")

- Minimal emitter returning WARN stub (keeps path present):
from sos.vmcore_report.emitters.registry import emits

@emits("proc/interrupts")
def emit_interrupts(prog) -> str:
    return "# vmcore-report: stub (interrupt table not reconstructable)\n"

This guide describes how sos vmcore-report works, how it interacts with emitters and plugins, and how to add new logic or file replicas with minimal friction while keeping outputs useful and predictable.
