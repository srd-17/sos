# `sos vmcore-report` execution flow (design + code walkthrough)

This document describes how `sos vmcore-report` works end-to-end, from CLI
invocation through drgn program setup, data collection, and archive creation.

It is intended for contributors who want to:
- understand where to plug in new collection logic
- debug failures when reconstructing data from a vmcore
- add/extend emitters and enumerators

---

## Goals and constraints

`vmcore-report` is designed to produce a sosreport-like archive **from a crash
dump**:

Inputs:
- `vmcore` (kernel memory snapshot)
- `vmlinux` (unstripped kernel image with debug symbols)

Key constraints:
- No access to the original live system runtime state beyond what is stored in the vmcore.
- No shell commands (`add_cmd_output`) and no filesystem copying (`add_copy_spec`).
- All reconstruction must be done by reading kernel memory and symbols using `drgn`.

---

## High-level pipeline

1. **CLI parsing**
2. **Policy + configuration selection**
3. **drgn program setup**
4. **Plugin execution**
5. **Emitter discovery + output generation (proc/sys/commands scopes)**
6. **Write outputs into staging directory**
7. **Create compressed archive**
8. **Print archive path + checksum**

---

## Key source modules

- CLI entry:
  - `bin/sos`
  - `sos/options.py` (argument parsing shared across subcommands)
- vmcore-report core:
  - `sos/vmcore_report/plugin.py`
  - `sos/vmcore_report/drgn_context.py`
- Collection plugins:
  - `sos/vmcore_report/plugins/kernel_info.py`
  - `sos/vmcore_report/plugins/vmcore_procfs.py`
  - `sos/vmcore_report/plugins/vmcore_sysfs.py`
  - `sos/vmcore_report/plugins/vmcore_commands.py`
- Emitters:
  - `sos/vmcore_report/emitters/registry.py`
  - `sos/vmcore_report/emitters/{proc,sys,commands}/*`
- Output:
  - `sos/vmcore_report/emitters/writer.py`

---

## What actually happens during collection

### 0) The “collector” model difference vs `sos report`

`sos report` uses plugin APIs like:
- `add_cmd_output()` (run a command and capture stdout)
- `add_copy_spec()` (copy files from disk)

`vmcore-report` does **not** use these, because it is not on the live system.
Instead, it produces archive files by calling **Python functions** (emitters)
which read kernel state via a `drgn.Program` and return strings.

The core model is:

- **Emitter**: `produce(prog) -> str` (or `produce(prog, **keys) -> str`)
- **Scope**: collection namespace: `proc`, `sys`, `commands`
- **Writer**: receives `(path, body)` and writes into staging tree

### 1) CLI invocation and options

Example:

```bash
python3 bin/sos vmcore-report --vmcore /path/to/vmcore --vmlinux /path/to/vmlinux
```

`bin/sos` dispatches to the vmcore-report implementation via `sos/options.py`
and the vmcore-report plugin framework.

### 2) Policy + configuration

The sos policy layer still runs to provide:
- archive naming conventions
- compression defaults
- metadata

However, it does **not** drive command/file collection like in live mode.

### 3) drgn Program setup (`drgn_context.py`)

This is the most important step.

`drgn_context.py`:
- creates a `drgn.Program`
- loads debug info from `vmlinux`
- loads the vmcore memory image
- validates core kernel symbols/types can be resolved

Emitters later do things like:
- `prog["init_task"]`
- `prog["vm_zone_stat"]`
- `prog.type("struct irq_desc")`

If debug info doesn’t match the vmcore kernel build, many emitters will fail and
fall back to stubs.

### 4) Plugin execution: what runs and in what order

vmcore-report uses dedicated plugins under `sos/vmcore_report/plugins/`.
Each plugin is an orchestrator for a collection domain.

Typical flow:
- `kernel_info` plugin emits core kernel identity / metadata
- `vmcore_procfs` plugin generates `proc/*` files via proc emitters
- `vmcore_sysfs` plugin generates `sys/*` files via sys emitters
- `vmcore_commands` plugin generates `sos_commands/*` via commands emitters

Each plugin:
- calls the emitter registry to generate `(path, body)` outputs for a scope
- feeds the output set to the writer

### 5) Emitter discovery and execution (`emitters/registry.py`)

Emitter discovery is dynamic: you add a module under the right directory and it
is picked up automatically.

#### Decorators
- `@emits("path")` for fixed paths
- `@emits_many("path/{key}/file", enumerator="enumerate_*")` for templated paths

#### Discovery
`list_emitters(scope)`:
- imports modules under `sos.vmcore_report.emitters.<scope>`
- finds callables annotated by the decorators (functions with `_emit_path` set)
- returns `Emitter(path, enumerator, func)` records

#### Execution
`run_scope(scope, prog, logger=None)`:
- iterates discovered emitters
- for templated emitters, calls the enumerator first and expands keys
- calls emitter functions and collects `(dest_path, body)` tuples

Best-effort behavior:
- any emitter exception yields a stub file rather than aborting the whole report
- this keeps the archive layout stable and makes missing reconstruction obvious

#### Enumerator behavior
Enumerators (e.g. `enumerate_pids`, `enumerate_cpus`) must:
- be deterministic (sort output)
- validate values (avoid interpreting pointers as integers)
- return dictionaries to format template paths

### 6) Writer behavior (`emitters/writer.py`)

The writer is the only layer that touches the filesystem.

Responsibilities:
- create directories under a staging root
- write text to files at the resolved paths (e.g. `proc/meminfo`)
- ensure correct path prefixing and archive layout
- provide consistent behavior across all scopes

### 7) Archive creation

After plugin execution finishes and the staging tree contains all generated files:
- the directory is packaged into a compressed archive (e.g. `.tar.xz`)
- sos prints location, size, and checksum

---

## `emitters/commands/` subdirectories and discovery depth

You can structure command emitters similar to `sos report` `sos_commands/` layout.

Supported today:
- `sos/vmcore_report/emitters/commands/<module>.py`
- `sos/vmcore_report/emitters/commands/<package>/...` as long as `<package>` has an `__init__.py`

Limitation:
- discovery is not fully recursive across arbitrary depths. One nested package
  level is supported (because `pkgutil.iter_modules()` enumerates packages),
  but “package inside package” may require additional discovery logic.

(If you want multi-level deep discovery like `commands/kernel/fs/...`, implement
recursive import traversal in `registry._iter_submodules()`.)

---

## Where to add new collection

### Add a new file under proc/

Create:
- `sos/vmcore_report/emitters/proc/<name>.py`

Use:

```python
from sos.vmcore_report.emitters.registry import emits

@emits("proc/<name>")
def emit_proc_name(prog) -> str:
    ...
```

### Add many files under proc/<pid>/

Use:

```python
from sos.vmcore_report.emitters.registry import emits_many

@emits_many("proc/{pid}/status", enumerator="enumerate_pids")
def emit_status(prog, pid: int) -> str:
    ...
```

### Add kernel “command-like” summaries

Use:
- `sos/vmcore_report/emitters/commands/`

Example target paths:
- `sos_commands/kernel/interrupts`
- `sos_commands/kernel/workqueue`
- `sos_commands/storage/nvme`

---

## Debugging tips

- Confirm `Program` can load types:
  - missing debug symbols often show up as `LookupError` in emitters
- Keep emitters robust:
  - wrap symbol/member access in `try/except`
  - emit clear stub reasons
- Always validate output values:
  - pointers coerced to ints are a common failure mode
- Use deterministic ordering:
  - helps compare archives between runs
- Inspect archives frequently:
  - `tar -tf /var/tmp/sosvmcore-*.tar.xz | sort`
  - `tar -xOf ... <base>/proc/<file> | head`

---

## Related docs

- `docs/vmcore-report-contributing-emitters.md`
- `docs/vmcore-report-design.md`
- `docs/vmcore-report-drgn-plugins.md`
