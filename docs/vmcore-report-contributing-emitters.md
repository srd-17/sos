# Contributing emitters to `sos vmcore-report`

This page explains **how to add new data-collection outputs** to `sos vmcore-report` by contributing **emitters**.

Unlike `sos report` (live system collection), `vmcore-report` reconstructs files from a **vmcore + vmlinux** using **drgn**. This means:

- You **cannot** run shell commands.
- You **cannot** copy files from the live filesystem.
- You **can** reconstruct data by reading kernel memory structures via `drgn.Program`.

Emitters are the unit of collection. They produce the text bodies for files stored in the final archive under:

- `proc/*`
- `sys/*`
- `sos_commands/*` (kernel-oriented summaries; not real command output)

---

## Architecture overview (where emitters fit)

High-level components:

- `sos vmcore-report` subcommand
  - loads a `drgn.Program` from `--vmcore` and `--vmlinux`
  - runs a set of **drgn plugins** (vmcore-report plugins)
- vmcore-report plugins
  - call `emitters.registry.run_scope()` for each scope
- emitters
  - functions that return `str` output to be written into the archive
- writer
  - writes `(path, content)` pairs to the staged archive directory
  - archives/compresses the result

---

## Directory layout

Emitters live under:

- `sos/vmcore_report/emitters/proc/` → files under `proc/*`
- `sos/vmcore_report/emitters/sys/` → files under `sys/*`
- `sos/vmcore_report/emitters/commands/` → files under `sos_commands/*` (vmcore equivalents)

Emitters are discovered dynamically: adding a new `*.py` file under the correct directory is usually sufficient.

Key files:

- `sos/vmcore_report/emitters/registry.py`
  - emitter decorators (`@emits`, `@emits_many`)
  - discovery + enumeration (`list_emitters()`, `run_scope()`)
- `sos/vmcore_report/emitters/writer.py`
  - writes emitter outputs to disk inside the archive tree

---

## The emitter API

### Fixed-path emitters: `@emits`

Use this for a single file output.

```python
from sos.vmcore_report.emitters.registry import emits

@emits("proc/cmdline")
def emit_proc_cmdline(prog) -> str:
    return "...\n"
```

Requirements:
- Must accept `prog` (`drgn.Program`) as the first argument.
- Must return a `str` (text body).
- Must not write to disk directly (the writer handles that).

### Templated-path emitters: `@emits_many`

Use this when one emitter produces many files, e.g. `proc/<pid>/status`.

```python
from sos.vmcore_report.emitters.registry import emits_many

@emits_many("proc/{pid}/status", enumerator="enumerate_pids")
def emit_status(prog, pid: int) -> str:
    ...
```

- The `enumerator` must exist in `emitters/registry.py` (e.g. `enumerate_pids`, `enumerate_cpus`).
- The function signature must accept keys emitted by the enumerator.

---

## Best practices

### 1) Prefer deterministic output
Reports should be reproducible:
- Sort lists (PIDs, CPUs, modules, mounts) before output where possible.
- Avoid depending on hash iteration order.

### 2) Be defensive across kernel versions
Kernel structs and symbols change frequently. Always guard access:

- Use `try/except` around symbol lookups: `prog["symbol"]`
- Use `hasattr()` checks for struct members
- Provide fallbacks when the first strategy fails

When data is not reconstructable, return a stub:

```python
return "# vmcore-report: stub (not reconstructable from vmcore) - <reason>\n"
```

### 3) Never assume a live system
Emitters must not:
- run subprocesses
- read `/proc` or `/sys` from the host
- read disk files from the host (unless explicitly part of vmcore inputs)

### 4) Keep output close to the real target format
For `/proc`-like files, match the exact formatting as much as possible:
- `proc/meminfo`, `proc/interrupts`, `proc/softirqs`, `proc/slabinfo` should resemble their live equivalents.

If exact parity is not possible, document what is best-effort.

### 5) Minimize external dependencies
Prefer:
- `drgn` core + `drgn.helpers.linux.*`
Avoid:
- importing `drgn_tools` at runtime

Porting logic from drgn-tools is encouraged, but it should be adapted/copied into `sos/vmcore_report` to keep `sos` self-contained.

---

## How to add a new emitter (step-by-step)

Example: implement `proc/vmstat`.

1) Create file:
- `sos/vmcore_report/emitters/proc/vmstat.py`

2) Add an emitter function:

```python
from sos.vmcore_report.emitters.registry import emits

@emits("proc/vmstat")
def emit_proc_vmstat(prog) -> str:
    ...
```

3) Use `drgn` helpers to read kernel statistics
- Typically `prog["vm_zone_stat"]` / `prog["vm_node_stat"]` and enum definitions.

4) Run `vmcore-report` and inspect archive output
- Confirm file exists in tarball and format is reasonable.

5) Iterate until stable.

---

## How to test locally (manual)

Generate a report:

```bash
python3 bin/sos vmcore-report --vmcore /path/to/vmcore --vmlinux /path/to/vmlinux
```

Inspect output files inside the archive:

```bash
tar -tf /var/tmp/sosvmcore-*.tar.xz | grep proc/vmstat
tar -xOf /var/tmp/sosvmcore-*.tar.xz <base>/proc/vmstat | head
```

---

## Common pitfalls

- **Huge PIDs / nonsense enumerations**: validate outputs when using helper iterators.
- **Per-cpu data**: requires `per_cpu_ptr()` and correct struct types.
- **Pointers vs integers**: don’t `int()` arbitrary objects without validation.
- **Kernel config differences**: e.g. sparse IRQ descriptors (maple tree vs radix tree) require runtime detection.

---

## Emitter review checklist

Before submitting:
- [ ] Output path matches scope (`proc/`, `sys/`, `sos_commands/`)
- [ ] Output is deterministic (sorted, stable ordering)
- [ ] Handles missing symbols/members gracefully
- [ ] Returns a stub if unrecoverable (no exceptions bubble up)
- [ ] Avoids importing drgn-tools at runtime
- [ ] Verified in a generated archive
