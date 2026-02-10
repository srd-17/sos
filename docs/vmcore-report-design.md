# sos vmcore-report: Design and Implementation

## Summary

`sos vmcore-report` is a new sos subcommand that generates a sos-style archive by analyzing an offline kernel crash dump (vmcore) using [drgn]. Unlike `sos report`, which executes commands and copies files from a live system via plugin specifications, `vmcore-report` uses drgn-based plugins to introspect kernel data structures directly from the vmcore. The resulting archive mirrors sos conventions (logs, manifest, optional encryption/compression) and is suitable for downstream triage tools and human review.

Key properties:
- Works on a vmcore (offline dump), not on a live system.
- Uses drgn to read kernel memory and symbols (DWARF and/or CTF).
- Separate plugin system specifically for drgn-based data collection.
- Produces sos-compatible output structure and metadata.


## Goals / Non-Goals

- Goals
  - Add a top-level `sos vmcore-report` subcommand.
  - Provide a minimal drgn plugin framework and two example plugins.
  - Use drgn only (no drgn-tools dependency) for vmcore/vmlinux loading.
  - Maintain sos archive conventions (logs, manifest, compression, checksums).

- Non-Goals (initial MVP)
  - Live analysis (this is exclusively vmcore-oriented).
  - Complete parity with all `sos report` command options or plugins.
  - Perfect cross-kernel coverage for every symbol layout (plugins are robust but best-effort).


## User-Facing Behavior

Command:
```
sos vmcore-report [options]
```

Core options:
- `--vmcore PATH` (required): path to vmcore (e.g., `/var/crash/.../vmcore`).
- `--vmlinux PATH` (optional): uncompressed kernel image with debuginfo for the crashed kernel.
- `--debuginfo-dir PATH` (multi): extra directories to search for debuginfo (vmlinux, `*.ko.debug`, etc.).
- `--plugins LIST` (legacy alias to only-plugins).
- `-e/--enable-plugins LIST`: explicitly enable these drgn plugins.
- `-o/--only-plugins LIST`: run only the listed plugins.
- `-n/--skip-plugins LIST`: skip listed plugins.
- `--experimental`: allow experimental plugins to auto-run.
- `--label LABEL`: include additional label in archive name.
- `--build`: keep uncompressed directory instead of a tarball.

Global sos options like `--tmp-dir`, `--compression-type`, encryption (`--encrypt-pass`/`--encrypt-key`), `--quiet`, `--verbose`, etc., are supported.


## Architecture Overview

The implementation introduces a new component package:
```
sos/vmcore_report/
  __init__.py           # SoSVmcoreReport component (CLI, runner)
  drgn_context.py       # drgn Program initialization aligned with drgn-tools
  plugin.py             # DrgnPluginBase, helpers, and metadata flags
  plugins/
    __init__.py
    kernel_info.py      # example: utsname, banner, taints
    sched_debug.py      # example: scheduler debug dump
```

### Component: SoSVmcoreReport

- Defined in `sos/vmcore_report/__init__.py`.
- `root_required = False` (offline dump analysis).
- Handles:
  - CLI option parsing and validation.
  - Archive setup via `SoSComponent.setup_archive(...)`.
  - drgn program initialization (`drgn_context.build_program()`).
  - Plugin discovery and selection.
  - Plugin execution and error handling.
  - Manifest/log integration and archive finalization.

The archive preserves standard sos directories:
- `sos_logs/` (sos internal logs)
- `sos_reports/` (manifest)
- `plugins/<plugin_name>/` (plugin outputs)
- Additional paths are allowed (e.g., `sys/kernel/debug/sched/debug` for scheduler dump).


### drgn Initialization (drgn-only)

Implemented in `sos/vmcore_report/drgn_context.py` (no external deps):

1. Create a `drgn.Program()`.
2. `prog.set_core_dump(vmcore)` — single argument (path or fd).
   - If `PermissionError`, fall back to `drgn.internal.sudohelper.open_via_sudo(vmcore, os.O_RDONLY)`.
3. Debuginfo loading (best-effort):
   - Prefer explicit files from `--vmlinux` and `--debuginfo-dir` (vmlinux, `*.ko.debug`).
   - Else try `prog.load_default_debug_info()` if available.
   - Else try `prog.load_debug_info(default=True)` or `prog.load_debug_info(main=True)` depending on drgn version.
   - Any debuginfo errors are non-fatal; plugins handle absent symbols gracefully.

This ensures upstream-friendly, drgn-only behavior without additional dependencies.


### Plugin Framework

Defined in `sos/vmcore_report/plugin.py`:
- `DrgnPluginBase`:
  - `plugin_name`, `description` (metadata).
  - `default_enabled = True`: auto-run unless skipped.
  - `experimental = False`: gated behind `--experimental`, unless explicitly enabled.
  - Lifecycle:
    - `setup(self, prog)`: optional pre-collect initialization.
    - `check_enabled(self, prog)`: runtime activation (default returns True).
    - `collect(self, prog)`: required — perform drgn data collection and write results.
  - Output helpers:
    - `write_text(relpath, content)`
    - `write_json(relpath, obj)`
    - `write_lines(relpath, lines)`
    - Outputs are relative to `plugins/<plugin_name>/` unless you intentionally write elsewhere (e.g., `sys/...` path).

### Plugin Selection Semantics

- `--only-plugins`: run only these plugins.
- `--enable-plugins`: ensure these plugins run even if optional/experimental.
- `--skip-plugins`: skip these plugins.
- `--experimental`: allow plugins marked `experimental=True` to auto-run.
- `--plugins`: treated as a legacy alias for “only-plugins”.

This mirrors `sos report` behavior conceptually, without mixing live plugins with drgn plugins.


### Execution Pipeline

1. Validate inputs, initialize archive.
2. Build drgn `Program` from vmcore.
3. Discover drgn plugins from `sos.vmcore_report.plugins`.
4. Filter by selection flags and experimental status.
5. For each plugin:
   - Instantiate with `outdir`, `logger`, and `archive`.
   - Check `check_enabled()`.
   - `setup()`, then `collect()`.
6. Add sos logs and manifest, finalize archive:
   - Compression is controlled by `--compression-type`.
   - Optional encryption via sos infrastructure.
   - Checksum generated (policy’s preferred hash).
7. Present final archive location and metadata to the user.


## Example Plugins

### kernel_info

- Collects:
  - utsname (sysname, nodename, release, version, machine, domainname).
  - `linux_banner` (if present).
  - Kernel taints via `drgn.helpers.linux.kernel_taint_flags` (best-effort).
- Bytes/str handling:
  - `string_()` can return bytes or str depending on drgn version, so the plugin normalizes the type before writing.

Outputs:
```
plugins/kernel_info/uts.json
plugins/kernel_info/uts.txt
plugins/kernel_info/banner.txt            (if present)
plugins/kernel_info/taints.json           (if available)
plugins/kernel_info/kernel_info.json      (combined)
```


### sched_debug

- Writes a scheduler debug dump analogous to `/sys/kernel/debug/sched/debug`:
  - Output path in archive: `sys/kernel/debug/sched/debug`.
- Resilience:
  - CPU iteration fallbacks: online → present → possible → `nr_cpu_ids` → 0.
  - Per-CPU runqueue symbol fallbacks: `runqueues` → `rq`.
  - Rich `try/except` coverage to handle missing symbols or partially corrupted state.
- Optional annotations:
  - Set `SOS_VMCORE_SCHED_DEBUG=1` for verbose explanatory comments.


## Error Handling and Logging

- Errors from individual plugins do not abort the entire run.
- Warnings and errors are included in `sos_logs/` and can be referenced from the archive.
- drgn debuginfo loading errors are best-effort; missing symbols may reduce output fidelity for affected plugins.


## Performance and Concurrency

- Plugins currently execute sequentially for determinism and to simplify error handling over a single vmcore context.
- Future work could introduce safe concurrency where drgn usage is read-only and thread-safe for selected tasks.


## Output Structure and Naming

- Archive name follows sos policy naming with prefix replacement:
  - Example: `sosvmcore-<host>-<timestamp>.tar.xz` (or build directory with `--build`).
- Includes:
  - `sos_logs/` (sos.log, ui.log)
  - `sos_reports/manifest.json`
  - `plugins/<plugin_name>/...`
  - Any additional paths explicitly written by plugins (e.g., `sys/...`).


## Limitations and Future Work

- Automatic vmlinux discovery is best-effort; explicit `--vmlinux` and `--debuginfo-dir` may be needed.
- Coverage of kernel features varies by kernel version and available debuginfo.
- Planned enhancements:
  - Additional drgn plugins (panic summary, tasks, memory maps, slab info, io scheduler, etc.).
  - Richer auto-discovery and debuginfo download/installation workflows.
  - Unit tests and integration tests with curated sample vmcores.
  - Sphinx docs integration (convert .md to .rst if desired).


## References

- drgn: https://github.com/osandov/drgn
