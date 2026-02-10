# Developing drgn-based plugins for sos vmcore-report

This guide explains how to create and test a new drgn-based plugin for `sos vmcore-report`. These plugins introspect a kernel vmcore via [drgn] and write results into the sos archive.

Contents:
- Plugin structure and lifecycle
- Plugin selection and defaults
- Writing outputs (paths and helpers)
- Bytes/str safety with drgn
- Resilient access patterns
- Local testing and verification
- Optional: gating and runtime checks
- Troubleshooting and tips


## 1) Where to put your plugin

Create a new module in:
```
sos/vmcore_report/plugins/<your_plugin>.py
```

Ensure the file defines exactly one `DrgnPluginBase` subclass. The discovery logic imports all modules under `sos.vmcore_report.plugins` and selects the first subclass found in each.

Example filenames:
- `panic_summary.py`
- `tasks.py`
- `slab_info.py`


## 2) Base class and lifecycle

Plugins subclass `DrgnPluginBase` from:
```
from sos.vmcore_report.plugin import DrgnPluginBase
```

Key attributes and hooks:

- Metadata:
  - `plugin_name` (optional): default is a snake_case transform of the class name.
  - `description` (optional): short, human-readable description.
  - `default_enabled = True` (optional): controls whether the plugin runs by default.
  - `experimental = False` (optional): if True, requires `--experimental` or explicit enable to run.

- Lifecycle:
  - `setup(self, prog)`: optional pre-collection step (default: no-op).
  - `check_enabled(self, prog) -> bool`: optional runtime activation test (default: True).
  - `collect(self, prog)`: required; perform all data collection and write outputs.

- Helpers for writing outputs (relative to `plugins/<plugin_name>/` by default):
  - `write_text(relpath, content)`
  - `write_json(relpath, obj)`
  - `write_lines(relpath, lines)`


## 3) Minimal plugin template

```python
# sos/vmcore_report/plugins/my_plugin.py

from sos.vmcore_report.plugin import DrgnPluginBase

class MyPlugin(DrgnPluginBase):
    plugin_name = "my_plugin"          # optional; defaults to 'my_plugin' for class 'MyPlugin'
    description = "Short description"  # optional
    default_enabled = True             # optional; runs by default
    experimental = False               # optional

    def check_enabled(self, prog):
        # Optional: only run if a required symbol exists, etc.
        try:
            _ = prog["some_symbol"]
            return True
        except Exception:
            return False

    def setup(self, prog):
        # Optional one-time initialization before collect()
        return

    def collect(self, prog):
        # Do drgn lookups; be defensive (vmcores can be partially corrupted)
        result = {}

        try:
            # Example symbol fetch:
            # sym = prog["some_symbol"]
            # result["value"] = int(sym.value_())
            pass
        except Exception as e:
            # Prefer not to raise; write a warning file instead if helpful
            self.write_text("WARNINGS.txt", f"lookup failed: {e}\n")

        # Write outputs under plugins/my_plugin/
        self.write_json("data.json", result)
        self.write_text("summary.txt", "Collection done\n")
```

Notes:
- Catch exceptions liberally to avoid aborting the entire vmcore-report run.
- Prefer `write_json()` for structured data and `write_text()` for human-readable summaries.


## 4) Bytes vs str with drgn

drgn’s `string_()` may return `bytes` or `str` depending on context and version. Always normalize to `str`:

```python
def to_text(s):
    if isinstance(s, (bytes, bytearray)):
        return s.decode("utf-8", "ignore")
    return s

# Example:
raw = some_char_array.string_()
txt = to_text(raw).strip()
```

Avoid direct concatenation of `str` and `bytes` (will raise `TypeError`).


## 5) Resilient access patterns

Symbols and layouts vary across kernels and vmcores. Some tips:

- Use try/except around each accessor:
  - Missing fields / changed struct layouts
  - Invalid pointers
  - Partial data due to crash corruption

- CPU iteration fallbacks (example pattern from `sched_debug`):
  1) `for_each_online_cpu(prog)`
  2) `for_each_present_cpu(prog)`
  3) `for_each_possible_cpu(prog)`
  4) `range(nr_cpu_ids)` if available
  5) last resort: `0`

- Per-CPU arrays/symbols often have different names across kernels. Try candidates:
  - Example: `runqueues` then `rq`

- When failing to fetch content, write a clear `[WARN]` or `[ERROR]` line to a plugin-specific log file so downstream analysis can reason about partial results.


## 6) Writing outputs

By default, the helpers write under:
```
plugins/<plugin_name>/
```

You can also target other paths in the archive if you intentionally need a canonical location (e.g., scheduler dumps):

```python
# Write to a custom path:
self.archive.add_string("content\n", "sys/kernel/debug/my_feature/dump.txt", mode="w")
```

Use JSON as the canonical structured format and include a concise text summary for quick human inspection.


## 7) Selecting and running plugins

Run `sos vmcore-report` with the necessary paths:

```bash
python3 bin/sos vmcore-report \
  --vmcore /path/to/vmcore \
  --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux
```

Selection flags:
- `--only-plugins <a,b,c>`: run only these
- `--enable-plugins <x,y>`: ensure these run even if optional/experimental
- `--skip-plugins <z>`: skip these
- `--experimental`: allow `experimental=True` plugins to run by default
- Legacy alias: `--plugins` is treated like `--only-plugins`

Examples:
```bash
# Run only your plugin:
python3 bin/sos vmcore-report --vmcore /path/to/vmcore --only-plugins my_plugin

# Enable your plugin plus defaults:
python3 bin/sos vmcore-report --vmcore /path/to/vmcore --enable-plugins my_plugin

# Skip your plugin (for comparison):
python3 bin/sos vmcore-report --vmcore /path/to/vmcore --skip-plugins my_plugin
```


## 8) Testing and verification

1) Basic run
```bash
python3 bin/sos vmcore-report \
  --vmcore /path/to/vmcore \
  --vmlinux /usr/lib/debug/lib/modules/$(uname -r)/vmlinux \
  --only-plugins my_plugin
```

2) Inspect results
- The command prints the final archive path, e.g.:
  ```
  /var/tmp/sosvmcore-<host>-<ts>.tar.xz
  ```
- Extract and inspect:
  ```
  mkdir /tmp/check && tar -C /tmp/check -xf /var/tmp/sosvmcore-*.tar.xz
  tree /tmp/check/sosvmcore-*/plugins/my_plugin
  cat /tmp/check/sosvmcore-*/plugins/my_plugin/summary.txt
  jq '.' /tmp/check/sosvmcore-*/plugins/my_plugin/data.json
  ```

3) Logs and manifest
- Look at `sos_logs/ui.log` and `sos_logs/sos.log` for warnings/errors.
- The `sos_reports/manifest.json` contains metadata about the run.

4) Debuginfo
- If symbols are missing, include explicit paths:
  ```
  --debuginfo-dir /usr/lib/debug
  --debuginfo-dir /path/to/ko.debugs
  ```
- When available, the loader integrates with local `drgn-tools` to find debuginfo similarly to its CLI.


## 9) Optional: runtime gating

- `default_enabled = False` to opt a plugin out of default runs (enable with `--enable-plugins`).
- `experimental = True` to require `--experimental` or explicit enable.
- `check_enabled(self, prog)` to disable at runtime if preconditions are not met (e.g. required symbol absent).


## 10) Troubleshooting

- `TypeError: can't concat str to bytes`
  - Normalize `string_()` return values to `str` (see bytes/str section).
- Missing symbols / `KeyError` from `prog["some_symbol"]`
  - Wrap in `try/except`.
  - Consider alternate symbol names or kernel version checks.
- No output or partial content
  - Review `sos_logs/ui.log` and `sos_logs/sos.log`.
  - Ensure `--vmlinux` and/or `--debuginfo-dir` paths are correct and readable.
- Permissions
  - `vmcore-report` does not require root; if the vmcore path is protected, the loader attempts to use `open_via_sudo` where supported.

## 11) Style and best practices

- Keep output deterministic and small by default.
- Provide a concise text summary plus structured JSON.
- Use clear relative paths (`plugins/<plugin>/file.json`) unless a canonical non-plugin path is warranted.
- Fail soft: write warnings and partial results instead of raising.
- Prefer short, readable code; add comments for tricky struct layouts or fallbacks.

---

With this guidance, you can quickly add robust, drgn-powered plugins to `sos vmcore-report` and validate them locally against real vmcores.

[drgn]: https://github.com/osandov/drgn
