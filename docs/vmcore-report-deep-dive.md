# sos vmcore-report: Deep Dive (Implementation, Integration, and Design Principles)

This document provides a comprehensive, end-to-end exploration of the `sos vmcore-report` subcommand: how it is structured, how it interacts with existing sos infrastructure, the detailed execution flow, failure handling, and the design principles used.

Audience:
- sos maintainers and contributors
- Platform integrators looking to understand portability and extension points
- Plugin authors building advanced drgn-based analysis


## 1. High-Level Intent

`sos vmcore-report` produces a standard sos archive (manifest, logs, content tree) by analyzing a kernel crash dump (vmcore) using the [drgn] Python library. It intentionally avoids executing live commands on the host system and instead reads in-memory kernel structures from the crash dump. This neatly complements `sos report`, which operates on the live system via copy/command specs.

Key guiding ideas:
- Reuse sos’s mature archive, logging, policy, and option parsing frameworks.
- Keep drgn-specific logic encapsulated; do not perturb live-system plugin APIs.
- Mirror `drgn-tools` vmcore/vmlinux loading patterns to reduce surprises.


## 2. Code Structure and Entry Points

New package:
```
sos/vmcore_report/
  __init__.py           # SoSVmcoreReport: CLI wiring, main runner
  drgn_context.py       # drgn Program initialization flow
  plugin.py             # DrgnPluginBase and helpers
  plugins/
    __init__.py
    kernel_info.py      # Example: utsname, linux_banner, taints
    sched_debug.py      # Example: sched/debug-like dump
```

Registered in the top-level sos command:
- `sos/__init__.py`: Adds the subcommand mapping
  - Key: `'vmcore-report'`
  - Value: `(sos.vmcore_report.SoSVmcoreReport, ['vmcore'])`

This leverages sos’s `SoS` class to surface a new subparser (same infra as `report`, `clean`, etc.), ensuring consistency for global options and help handling.


## 3. CLI Parsing and Options

Location: `sos/vmcore_report/__init__.py`, class `SoSVmcoreReport.add_parser_options()`.

Supports:
- Required: `--vmcore PATH`
- Optional:
  - `--vmlinux PATH`
  - `--debuginfo-dir PATH` (multi)
  - Selection flags (mirroring sos report semantics):
    - `--plugins LIST` (legacy alias for only-plugins)
    - `-o/--only-plugins LIST`
    - `-e/--enable-plugins LIST`
    - `-n/--skip-plugins LIST`
    - `--experimental`
  - Archive behavior: `--build`
  - Labeling: `--label LABEL`

Also inherits global sos options (via `SoSComponent._add_common_options()`), e.g., `--tmp-dir`, `--compression-type`, `--encrypt-key/--encrypt-pass`, `--quiet`, `--verbose`, `--threads`, etc.

Important difference vs. `sos report`:
- `vmcore-report` deliberately avoids live-system collection options (e.g., `--chroot`, `--skip-files`, `--skip-commands`, etc.).
- Root is NOT required (`root_required = False`) because vmcore reading is offline; privileged open may still be necessary for certain paths (handled by drgn via `sudohelper` if available).


## 4. Execution Pipeline (Detailed)

Orchestrated by: `SoSVmcoreReport.execute()`.

Steps:
1. Print header (banner includes sos version).
2. Policy integration: `self.policy.set_commons(...)`
   - Reuses sos’s policy for output paths, tmp directory selection, hash preferences, and result presentation.
3. Input validation: ensure `--vmcore` exists; if `--vmlinux` given, verify it exists; warn on invalid `--debuginfo-dir`.
4. Archive setup:
   - Calls `self.setup_archive(name=...)` (from `SoSComponent`):
     - Uses sos `Archive` classes (FileCacheArchive -> TarFileArchive) based on `--compression-type` and policy prefs.
     - Creates sos directories: `sos_logs/`, `sos_reports/`, plus a plugin tree `plugins/`.
     - Handles encryption when requested (key/pass) and configures permissions via umask.
5. drgn Context Build: `drgn_context.build_program()`
   - Aligns with `/home/opc/drgn-tools` CLI patterns:
     - `prog = drgn.Program()`
     - `prog.set_core_dump(vmcore)` (single-argument version; avoids legacy signatures)
     - If set_core_dump raises `PermissionError`, attempt `drgn.internal.sudohelper.open_via_sudo(vmcore, os.O_RDONLY)` and pass fd.
   - Debuginfo Loading Strategy:
     - If `drgn-tools` is importable:
       - Registers drgn-tools debuginfo finders and sets caches (`opts`, `vmcore_path`).
       - Tries explicit `vmlinux` and `*.ko.debug` from `--debuginfo-dir`.
       - Else: `prog.load_debug_info(default=True)`; fallback to `main=True`.
     - If not:
       - Try explicit file list (vmlinux and nested `*.ko.debug`).
       - Else: `prog.load_default_debug_info()` or fallback to `main=True`.
     - Errors during debuginfo loading are best-effort and logged at debug; plugin collectors handle symbol gaps gracefully.
6. Plugin Discovery and Selection:
   - `ImporterHelper(sos.vmcore_report.plugins).get_modules()` lists plugin modules.
   - For each module:
     - Import and filter to the first `DrgnPluginBase` subclass.
     - Apply selection gates in this order:
       - `only-plugins` (incl. legacy `--plugins`)
       - `skip-plugins`
       - `experimental` gating unless explicitly enabled
       - `default_enabled` (if `False`, requires enable/only to run)
     - At runtime, per plugin instance, call `check_enabled(prog)` before `collect()`; if `False` mark as "inactive".
   - Differences from `sos report`:
     - No policy-specific plugin subclasses (drgn plugins are single tree, currently policy-agnostic).
     - The selection semantics conceptually mirror sos, but collection semantics are drgn-based rather than command/copy specs.
7. Plugin Execution (sequential):
   - For each selected plugin:
     - Construct with `outdir`, `logger`, `archive`.
     - `setup(prog)`; then `collect(prog)`.
     - Errors are caught and logged; the run proceeds to the next plugin.
     - Sequential (single-threaded) execution is chosen for simplicity and determinism; potential future optimization could consider concurrency only if drgn and plugin logic are proved safe for parallel reads.
8. Manifest and Logs:
   - Archive receives sos logs and a generated manifest (`archive.add_final_manifest_data()`).
   - `--quiet` and UI logging integrate with sos’s existing logging model (ui.log goes to stdout; logs are also included in the archive).
9. Finalization:
   - Compress (or move build directory) and compute checksum using the policy’s preferred hash.
   - Display results via `self.policy.display_results(archive, directory, checksum, ...)` for consistent UX with sos.
   - Optional upload step can be integrated post-archive creation (reuses sos upload infrastructure).


## 5. Deep Integration Points with Existing sos

- CLI Framework:
  - Uses top-level `SoS` parser and subparser creation, making help/layout consistent.
- SoSComponent Base:
  - Leverages `SoSComponent` initialization for:
    - Logging setup (sos.log, ui.log handlers, verbosity).
    - Temp file management (`TempFileUtil`) and tmp dir selection (via policy).
    - Manifest root creation and finalization.
  - Honors `--encrypt`, `--encrypt-key`, `--encrypt-pass`, `--compression-type`, `--tmp-dir`, and `--quiet` consistently.
- Archive Handling:
  - Reuses `FileCacheArchive` / `TarFileArchive`:
    - Name handling, content addition (strings, links, files).
    - Permission copying and SELinux context capture (where applicable).
    - `finalize()` path with compression, output file creation, checksum generation and migration to system tmp dir.
- Policy Interactions:
  - `get_archive_name()` to obtain base name used by `vmcore-report` with a prefix substitution: `"sosreport-"` → `"sosvmcore-"`.
  - Hash preferences for checksum, and result display.
  - tmp dir selection and runtime environment introspection where needed.
- Utilities and Helpers:
  - `ImporterHelper` for plugin module discovery.
  - Options merging is inherent in `SoSComponent` and `SoSOptions`; however `vmcore-report` does not use presets for defaults at this time (unlike `sos report` where presets can alter behavior drastically).
- Differences vs `sos report`:
  - No live command or file copy specs; no chroot or namespace wrangling.
  - Runtime device enumeration, profiles, and policy-tailored plugins are not used here.
  - Focus on offline introspection through drgn.


## 6. Plugin System (Drgn-Specific)

Defined in `sos/vmcore_report/plugin.py`:

- `DrgnPluginBase`:
  - Metadata:
    - `plugin_name` (defaults to snake_case of class name).
    - `description` for help listings.
    - `default_enabled` (True): runs unless skipped.
    - `experimental` (False): requires `--experimental` to auto-run.
  - Hooks:
    - `setup(prog)`: optional plugin-specific initialization.
    - `check_enabled(prog) -> bool`: runtime gating (e.g., require a symbol).
    - `collect(prog)`: implement the drgn analysis and write outputs.
  - Writers:
    - `write_text()`, `write_json()`, `write_lines()`:
      - Outputs under `plugins/<plugin_name>/` by default.
      - For canonical system paths (e.g. `sys/kernel/debug/sched/debug`) use `archive.add_string()` with the explicit path.

Selection semantics honor:
- `--only-plugins`, `--enable-plugins`, `--skip-plugins`, `--experimental`, and legacy `--plugins` (alias for only).


## 7. drgn Program and Debuginfo Strategy

Located at `sos/vmcore_report/drgn_context.py`.

Key considerations:
- The authoritative vmcore open call is:
  - `Program().set_core_dump(vmcore)` — No additional positional/keyword arguments are passed to avoid incompatibilities across drgn versions (legacy signatures differed).
- If `PermissionError` occurs on protected paths (`/proc/kcore`), fallback uses `drgn.internal.sudohelper.open_via_sudo()`.
- Debuginfo loading:
  - With `drgn-tools`:
    - Register finders and place options in `prog.cache`.
    - Attempt to load provided `vmlinux` and `*.ko.debug` under `--debuginfo-dir`.
    - Else, fall back to `load_debug_info(default=True)` or `main=True`.
  - Without `drgn-tools`:
    - Attempt explicit list, then system defaults where supported.
- Failure strategy:
  - Debuginfo is best-effort; do not abort on missing DWARF/CTF or module mismatches.
  - Plugin code should handle missing symbols robustly.


## 8. Example Plugins (Behavior and Rationale)

### kernel_info
- Reads:
  - `init_uts_ns.name` (sysname, nodename, release, version, machine, domainname).
  - `linux_banner` (if present).
  - Kernel taints via `drgn.helpers.linux.kernel_taint_flags`.
- Bytes/str safety:
  - `string_()` can be bytes or str; the plugin normalizes before concatenation or writing.
- Outputs:
  - `plugins/kernel_info/uts.json`, `uts.txt`, `banner.txt`, `taints.json`, combined `kernel_info.json`.

### sched_debug
- Scheduler state summary akin to `/sys/kernel/debug/sched/debug`.
- CPU discovery fallbacks: `online → present → possible → nr_cpu_ids → 0`.
- Runqueue symbol fallbacks: `runqueues → rq`.
- Defensive fetch for every field and structure.
- Output path:
  - Writes directly to `sys/kernel/debug/sched/debug` within the archive to match user expectations.
- Optional annotations via `SOS_VMCORE_SCHED_DEBUG=1`.


## 9. Error Handling, Logging, and Manifest

- Errors in plugin code:
  - Logged and do not stop the run.
  - Provide file artifacts (e.g., WARNINGS/ERRORS text) in plugin directories for debugging.
- Logging:
  - Uses sos logging infrastructure:
    - `sos.log` (developer-focused)
    - `ui.log` (user-facing progress and messages)
  - Logs are included in `sos_logs` in the final archive.
- Manifest:
  - sos manifest is created at run start and finalized by `Archive.add_final_manifest_data()`.
  - Contains:
    - sos version, cmdline, start/end/run-time, compression.
    - The manifest JSON is accessible at `sos_reports/manifest.json`.
  - vmcore-report currently uses a simpler manifest than `sos report` (no live-device scans, etc.).


## 10. Security Considerations

- `root_required = False`: avoids unnecessary privilege prompts for offline analysis.
- Archive creation respects umask and moves final tarball with secure rename semantics similar to `sos report`.
- Encryption and hash generation:
  - Reused sos encryption (GPG key or passphrase) and checksum writing.
- Avoids live-system changes; does not execute live commands nor read sensitive runtime paths unless specifically requested by the user and accessible.


## 11. Performance and Concurrency

- Chosen to run sequentially for deterministic behavior with a single drgn `Program`:
  - Avoids contention or subtle races inside symbol resolution or Program caches.
  - Common vmcore operations are bound by IO/parse operations; concurrency is unlikely to yield immediate benefits without refactoring.


## 12. Design Principles Applied

- Separation of Concerns:
  - `drgn_context.py` isolates Program/debuginfo decisions from CLI orchestration.
  - `plugin.py` provides a minimal, drgn-oriented base distinct from live-system `Plugin` APIs.
- Compatibility and Least Surprise:
  - CLI and archive semantics mirror sos.
  - `drgn-tools` integration avoids reinventing debuginfo finder logic and improves user continuity.
- Robustness and Best Effort:
  - Missing symbols, shifting kernel structures, or partial dumps should not abort runs.
  - Plugins log and continue with partial results whenever feasible.
- Extensibility:
  - Plugins discovered via `ImporterHelper` and filtered via familiar switches.
  - Minimal requirements for a new drgn plugin: subclass, implement `collect()`.
- Minimal Coupling:
  - No cross-dependency on live-system plugin set or profiles.
  - Abstains from policy-specific code paths unless beneficial (e.g., naming, hashing, tmp dir).
- Principle of Least Privilege:
  - Non-root by default; privileged operations are avoided except when drgn internally requires special handling (via `sudohelper`).
- Observability:
  - Logs and structured outputs aim to support both humans and automated analysis (e.g., Insights-like tools).


## 13. Key Differences vs `sos report`

- Source of truth:
  - `sos report`: live host state via commands and copied files.
  - `vmcore-report`: offline state via vmcore introspection.
- Plugin model:
  - `sos report`: add_cmd_output/add_copy_spec; profile/policy dependent.
  - `vmcore-report`: drgn API usage; single plugin tree; policy-agnostic today.
- Runtime requirements:
  - `sos report`: root required by default to access system files/logs.
  - `vmcore-report`: root not required; depends on vmcore path access.


## 14. Future Enhancements

- Additional drgn plugins (panic summary, per-task dumps, memory maps/slab, IO stats).
- Auto-discovery improvements for vmlinux and module debuginfo.
- Optional parallel execution where safe.
- Enhanced manifest content for vmcore context (e.g., panic reason, call stacks index).
- Test harness with sample vmcores and CI gating.


## 15. File/Function Reference

- CLI + Runner:
  - `sos/vmcore_report/__init__.py` → `SoSVmcoreReport`
    - `add_parser_options()`
    - `execute()`
    - `_validate_inputs()`, `_prepare_archive()`
    - `_init_drgn()`: calls `drgn_context.build_program()`
    - `_discover_plugins()`: selection logic
    - `_run_plugins()`: sequential execution
- drgn Context:
  - `sos/vmcore_report/drgn_context.py` → `build_program()`
    - `Program()`; `set_core_dump(vmcore|fd)`
    - Optional drgn-tools registration and debuginfo loading
- Plugin API:
  - `sos/vmcore_report/plugin.py` → `DrgnPluginBase`
    - `plugin_name`, `description`, `default_enabled`, `experimental`
    - `setup()`, `check_enabled()`, `collect()`
    - `write_text()`, `write_json()`, `write_lines()`
- Example Plugins:
  - `sos/vmcore_report/plugins/kernel_info.py`
  - `sos/vmcore_report/plugins/sched_debug.py`


## 16. Operational Runbook (Summary)

1. Ensure `drgn` (and optionally `drgn-tools`) is installed; provide `--vmlinux` and `--debuginfo-dir` when needed.
2. Run:
   ```
   python3 bin/sos vmcore-report --vmcore /path/to/vmcore \
     --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux
   ```
3. Inspect final archive path and checksum printed to the terminal.
4. Extract and examine:
   - `plugins/*/` outputs
   - `sys/...` paths written by plugins (e.g., `sys/kernel/debug/sched/debug`)
   - `sos_logs/*`, `sos_reports/manifest.json`


---

This deep dive describes not only what `sos vmcore-report` does, but also how each part connects to sos’s architecture and why those choices were made.

[drgn]: https://github.com/osandov/drgn
