# sos `report` execution flow: architecture, initialization, and how to add subcommands

This page is intentionally **very deep**: it explains not just “what calls what”, but **what every major object is, what fields it initializes, what each field is used for later, and where the data flows**.

If you want to extend sos (add a subcommand/component, add new options, change report behavior, or debug plugins), the goal is that you can do it by following the references and patterns below.

> Reading strategy
>
> 1. Read the **execution timeline** first to understand ordering.
> 2. Then read the **object/attribute tables** (SoS / SoSComponent / SoSReport).
> 3. Then read the **data flow** sections (options merge, policy selection, plugin selection, archive lifecycle).
> 4. Finally, use the **“add a subcommand”** recipe at the end.

---

## Table of contents

1. [Execution timeline (what runs when)](#execution-timeline-what-runs-when)
2. [Entry point: `bin/sos`](#entry-point-binsos)
3. [Dispatcher: `sos.SoS` (component registry + argparse)](#dispatcher-sossos-component-registry--argparse)
   1. [`SoS.__init__` in detail](#sos__init__-in-detail)
   2. [`SoS._components`: what it is and how to extend it](#sos_components-what-it-is-and-how-to-extend-it)
   3. [Common options: why they are in the dispatcher](#common-options-why-they-are-in-the-dispatcher)
   4. [`SoS._init_component`: root checks and construction](#sos_init_component-root-checks-and-construction)
4. [Framework base: `SoSComponent` (policy/options/logging/tmp/manifest)](#framework-base-soscomponent-policyoptionsloggingtmpmanifest)
   1. [`SoSComponent` lifecycle: the *real* initialization ordering](#soscomponent-lifecycle-the-real-initialization-ordering)
   2. [SoSComponent attribute reference (every important field)](#soscomponent-attribute-reference-every-important-field)
   3. [Options deep dive: `SoSOptions` and precedence rules](#options-deep-dive-sosoptions-and-precedence-rules)
   4. [Logging deep dive (soslog vs ui_log)](#logging-deep-dive-soslog-vs-ui_log)
   5. [Tempdir, TempFileUtil, and safety constraints](#tempdir-tempfileutil-and-safety-constraints)
   6. [Archive construction and encryption flow](#archive-construction-and-encryption-flow)
   7. [Manifest/metadata deep dive (`SoSMetadata`)](#manifestmetadata-deep-dive-sosmetadata)
5. [`sos report`: `SoSReport` (plugin runtime + pipeline)](#sos-report-sosreport-plugin-runtime--pipeline)
   1. [SoSReport attributes reference](#sosreport-attributes-reference)
   2. [`SoSReport.__init__` step-by-step](#sosreport__init__-step-by-step)
   3. [`SoSReport.execute` pipeline step-by-step](#sosreportexecute-pipeline-step-by-step)
   4. [Plugin discovery + selection mechanics (and how policy participates)](#plugin-discovery--selection-mechanics-and-how-policy-participates)
   5. [Plugin setup stage: what gets injected and why](#plugin-setup-stage-what-gets-injected-and-why)
   6. [Plugin collect stage: threading, timeouts, and progress UI](#plugin-collect-stage-threading-timeouts-and-progress-ui)
   7. [Report rendering stage (text/json/html)](#report-rendering-stage-textjsonhtml)
   8. [Post-processing stage (postproc)](#post-processing-stage-postproc)
   9. [Finalization stage: cleaner, manifest, checksums, move, upload, cleanup](#finalization-stage-cleaner-manifest-checksums-move-upload-cleanup)
6. [Policy deep dive: how sos picks a policy and what policy controls](#policy-deep-dive-how-sos-picks-a-policy-and-what-policy-controls)
7. [Design patterns (precise mapping to code)](#design-patterns-precise-mapping-to-code)
8. [How to add a new sos subcommand (component) after reading this](#how-to-add-a-new-sos-subcommand-component-after-reading-this)

---

## Execution timeline (what runs when)

This is the canonical order for `sos report ...`:

1. **Process entry**
   * `bin/sos` runs and imports `SoS`
2. **Dispatcher setup**
   * `SoS.__init__(argv)` registers components and builds argparse parsers
   * `argparse.parse_args(argv)` decides which component is requested and parses all arguments
3. **Component construction**
   * `SoS._init_component()` constructs `SoSReport(parser, args, argv)`
4. **Framework initialization** *(this is most of “initialization”)*
   * `SoSComponent.__init__` runs:
     * signals
     * base `SoSOptions` creation
     * policy load (optional)
     * config/preset/cmdline option merge
     * tmpdir creation (if logging enabled)
     * logging handler setup
     * manifest root construction
5. **Report-specific initialization**
   * `SoSReport.__init__` runs:
     * header/debug mode
     * sysroot/chroot mode sanity checks
     * archive internal directory names (`sos_commands`, `sos_logs`, `sos_reports`)
     * device + namespace inventory
6. **Execution**
   * `SoS.execute()` calls `SoSReport.execute()`
   * `SoSReport.execute()` runs pipeline stages:
     1. policy commons wiring
     2. plugin discovery/selection
     3. tunable application (`--alloptions`, `--plugopts`)
     4. listing/preset operations (may exit early)
     5. interactive/batch gate
     6. prework (archive creation + mkdirs)
     7. manifest global data add
     8. plugin setup
     9. plugin collect (threaded)
     10. env vars collection
     11. summary report rendering
     12. plugin postproc
     13. version file
     14. finalization (clean/checksum/move/upload/cleanup)
7. **Hard exit**
   * `bin/sos` calls `os._exit(0)` after component returns

---

## Entry point: `bin/sos`

**File:** `bin/sos`

```python
sys.path.insert(0, os.getcwd())
from sos import SoS
...
sos = SoS(sys.argv[1:])
sos.execute()
os._exit(0)
```

### What it initializes and why

* `sys.path.insert(0, os.getcwd())`
  * Ensures that if you run from a git checkout, imports resolve to that checkout.
  * Without this, you might import an installed `sos` instead of your local tree.

* `SoS(sys.argv[1:])`
  * Constructs the dispatcher and parses args immediately.
  * Side effect: this already constructs the chosen component object.

* `os._exit(0)`
  * Ensures no “straggler” threads or logging handlers keep the interpreter alive.
  * **Important implication for component authors:** do not rely on normal interpreter cleanup; your component should do its own cleanup (tmpdir removal, handler shutdown, etc.) or rely on existing framework cleanup.

---

## Dispatcher: `sos.SoS` (component registry + argparse)

**File:** `sos/__init__.py`  
**Class:** `SoS`

`SoS` has exactly two jobs:

1. Build a top-level CLI parser with subcommands.
2. Construct the chosen subcommand component and call its `execute()`.

### `SoS.__init__` in detail

`SoS.__init__(args)`:

1. Stores argv (excluding program name) in `self.cmdline`.
2. Imports each component module (so their classes exist).
3. Builds `self._components` mapping.
4. Builds a help/usage string listing available components and their descriptions (`ComponentClass.desc`).
5. Creates `ArgumentParser` for top-level `sos`.
6. Creates `subparsers = parser.add_subparsers(dest='component', ...)` and marks required.
7. For every component:
   1. Creates a subparser: `subparsers.add_parser(comp, aliases=...)`
   2. Adds common options: `_add_common_options(subparser)`
   3. Calls component hook: `ComponentClass.add_parser_options(parser=subparser)`
   4. Sets default `component=comp`
8. Parses args: `self.args = self.parser.parse_args(self.cmdline)`
9. Initializes component instance: `self._init_component()`

#### Why does it build all component subparsers up front?

So that `sos <component> --help` works without needing a second stage parser.

### `SoS._components`: what it is and how to extend it

`self._components` is:

```python
{
  'report': (sos.report.SoSReport, ['rep']),
  'clean': (sos.cleaner.SoSCleaner, ['cleaner', 'mask']),
  ...
}
```

Each entry defines:

* canonical command string (`report`)
* the class to instantiate (`SoSReport`)
* aliases accepted by argparse (e.g. `rep`)

**Extending**: add an import and add one entry.

### Common options: why they are in the dispatcher

`SoS._add_common_options(parser)` adds flags shared by all components.

This is done at dispatcher time, not component time, because:

* argparse subparsers must be configured before parse_args
* shared flags should look uniform across components
* some options affect framework initialization (tmpdir, sysroot, debug verbosity)

### `SoS._init_component`: root checks and construction

Key logic:

* Determine requested component: `self.args.component`
* Root enforcement:
  * checks `ComponentClass.root_required`
  * if root required and `os.getuid() != 0`, initialization fails early

Then instantiate:

```python
self._component = ComponentClass(self.parser, self.args, self.cmdline)
```

**This is where SoSComponent initialization happens** (policy, options, logging, tmpdir, manifest).

---

## Framework base: `SoSComponent` (policy/options/logging/tmp/manifest)

**File:** `sos/component.py`  
**Class:** `SoSComponent`

Think of `SoSComponent` as:

* the base “application framework” for sos commands
* the place where “global guarantees” are established:
  * how options are loaded and overridden
  * where logs go
  * where temporary workspace lives
  * what policy is in effect
  * where manifest is recorded

### `SoSComponent` lifecycle: the *real* initialization ordering

This is the step-by-step “what initializes what”:

#### Step 0: basic fields

```python
self.parser = parser
self.args = parsed_args
self.cmdline = cmdline_args
self.exit_process = False
self.archive = None
self.tmpdir = None
self.tempfile_util = None
self.manifest = None
```

What these mean:

* `parser`: the top-level argparse parser (not only subparser). Used later to re-parse cmdline overrides safely.
* `args`: the parsed `Namespace` from argparse. This is *not* the final options source (that becomes `self.opts`).
* `cmdline`: original argv list, used for:
  * logging the full invocation
  * re-parsing to detect explicit overrides
  * upload component initialization
* `exit_process`: flag set when SIGTERM happens; used in exception handling flows.
* `archive`: archive object (created later by `setup_archive`)
* `tmpdir`: run-private temporary root directory (`/var/tmp/sos.<random>`)
* `tempfile_util`: helper for creating temporary files inside `tmpdir`
* `manifest`: SoSMetadata root (JSON manifest structure)

#### Step 1: SIGTERM handler

```python
signal.signal(signal.SIGTERM, self.get_exit_handler())
```

`get_exit_handler()` returns a closure that sets:

* `self.exit_process = True`
* calls `_exit()` which raises SystemExit

Purpose:

* Ensures container orchestration / system shutdown can terminate sos cleanly.

#### Step 2: initial SoSOptions object

```python
self.opts = SoSOptions(arg_defaults=self._arg_defaults)
```

At this moment, `self.opts` is *only* the base defaults (global options), not yet merged with component defaults.

#### Step 3: policy load (optional)

If `load_policy = True` (default):

```python
self.load_local_policy()
```

`load_local_policy` calls:

```python
self.policy = sos.policies.load(sysroot=self.opts.sysroot, probe_runtime=self.load_probe)
self.sysroot = self.policy.sysroot
self._is_root = self.policy.is_root()
```

Important subtlety:

* It passes `self.opts.sysroot` **before options are fully merged**.
* Typically, this is `None`, so policy loads with its own default sysroot unless overridden later.
* Later, SoSReport may update sysroot behavior based on options/policy/container.

#### Step 4: merge component arg_defaults into global defaults

```python
self._arg_defaults.update(self.arg_defaults)
```

Each component defines its own `arg_defaults` dict; the base has `_arg_defaults` with global ones.

This update:

* expands known option names
* defines types for config conversion (`SoSOptions._convert_to_type`)

#### Step 5: load final effective options

```python
self.opts = self.load_options()
```

This is one of the most important calls. See [Options deep dive](#options-deep-dive-sosoptions-and-precedence-rules).

Result:

* `self.opts` becomes the single source of truth for *effective* options.

#### Step 6: logging/tmpdir/manifest setup (optional)

If `configure_logging = True` (default):

1. Determine system temp dir (`sys_tmp`) via:

   ```python
   tmpdir = self.get_tmpdir_default()
   ```

   That selects:
   * `--tmp-dir` if provided
   * else env var `TMPDIR`
   * else `policy.get_tmp_dir(None)` (typically `/var/tmp` or platform appropriate)

   It also determines filesystem type via `stat` and warns if tmpfs.

2. Create manifest root:

   ```python
   self.manifest = SoSMetadata()
   ```

3. Validate tmpdir exists and is writable; otherwise exit before logging exists.

4. Create the run-private directory:

   ```python
   self.tmpdir = tempfile.mkdtemp(prefix="sos.", dir=self.sys_tmp)
   ```

5. Create tempfile helper:

   ```python
   self.tempfile_util = TempFileUtil(self.tmpdir)
   ```

6. Setup logging handlers:

   ```python
   self._setup_logging()
   ```

#### Step 7: baseline manifest fields

If manifest exists:

* `version`, `cmdline`, `start_time`
* placeholders for `end_time`, `run_time`, `compression`
* `tmpdir` and `tmpdir_fs_type`
* `policy` name
* `components` section (a nested SoSMetadata)

---

### SoSComponent attribute reference (every important field)

| Attribute | Set in | Type | Meaning | Where used later |
|---|---:|---|---|---|
| `parser` | `__init__` | `argparse.ArgumentParser` | Root parser used to re-parse cmdline | `apply_options_from_cmdline()` |
| `args` | `__init__` | `argparse.Namespace` | Raw argparse parse result | used for `config_file`, `component` |
| `cmdline` | `__init__` | `list[str]` | Original argv (excluding prog) | logging, preset merge logging, uploader init |
| `opts` | `load_options()` | `SoSOptions` | Effective merged options | everywhere |
| `policy` | `load_local_policy()` | `Policy` | Distro/platform behavior provider | archive naming, plugin match/validate, tmpdir, presets, etc. |
| `sysroot` | `load_local_policy()` | `str` | Policy sysroot, may change in report logic | used for archive, command execution paths |
| `archive` | `setup_archive()` | archive class | Where collected data is written | report prework/setup/collect/finalize |
| `sys_tmp` | logging init | `str` | Public temp dir (e.g. `/var/tmp`) where final outputs are moved | final_work move+display_results |
| `tmpdir` | logging init | `str` | Private run temp root (removed at end) | archive root is created inside here |
| `tempfile_util` | logging init | `TempFileUtil` | Creates temp files in tmpdir | log file handles, report renders |
| `soslog` | `_setup_logging()` | logger | verbose log w timestamps, stored in archive | debug and postmortem |
| `ui_log` | `_setup_logging()` | logger | user-facing messages | progress and results |
| `manifest` | logging init | `SoSMetadata` | run metadata tree | written into archive final manifest |
| `preset` | `load_options()` | `PresetDefaults|None` | policy preset applied | report logs effective option set |
| `_arg_defaults` | class var | dict | global + component defaults | types and defaults for SoSOptions |

---

### Options deep dive: `SoSOptions` and precedence rules

**File:** `sos/options.py`  
**Class:** `SoSOptions`

#### What SoSOptions really is

`SoSOptions` is a mutable object whose attributes are option names (e.g. `threads`, `clean`, `profiles`, `plugopts`, etc).

It carries two key pieces of metadata:

* `arg_defaults`: dict of default values (and types)
* `_nondefault`: set of options that have been explicitly changed from defaults during merges

#### Precedence chain in SoSComponent.load_options()

`SoSComponent.load_options()` (in `sos/component.py`) creates and merges options in this order:

1. Start with defaults (global+component) by creating:

   ```python
   opts = SoSOptions(arg_defaults=self._arg_defaults)
   ```

2. Reset argparse defaults to `None` (except SUPPRESS), so “not set on cmdline” stays `None`. This prevents argparse defaults from clobbering config file values.

3. Load config file sections:

   ```python
   opts.update_from_conf(self.args.config_file, self.args.component)
   ```

   Special case: if cmdline contains `--clean` or `--mask`, also load the `clean` section.

   Another special case: if non-root, load `~/.config/sos/sos.conf` if present.

4. Apply cmdline overrides *only where user explicitly specified*:

   ```python
   opts = self.apply_options_from_cmdline(opts)
   ```

   `apply_options_from_cmdline` works by:
   * parsing cmdline again via argparse
   * then building a SoSOptions where defaults are the *current config-derived* values
   * comparing and only applying those that differ

   This is subtle but critical: it means “cmdline wins, but only for args the user actually set”.

5. Presets: if component supports `preset`, then:
   * find preset if user requested one
   * else probe preset
   * merge preset opts into opts, with `prefer_new=True` to apply preset defaults
   * re-apply cmdline overrides so cmdline wins over preset too

#### Why list options behave differently

In `SoSOptions._merge_opt`, sequences are concatenated in some cases (instead of overwritten). That supports patterns like:

* config defines some `skip_plugins`, cmdline adds additional ones
* plugin option list `plugopts` should accumulate

But then `apply_options_from_cmdline` has special reconciliation for:
* `enable_plugins`, `skip_plugins`, `only_plugins`
* `plugopts` (drop config/preset value if cmdline redefines same option name)

That code exists because concatenation alone would create invalid combined sets.

---

### Logging deep dive (soslog vs ui_log)

`SoSComponent._setup_logging()` creates two loggers:

* `soslog` (logger name `sos`):
  * DEBUG level logger; file handler at INFO by default
  * console handler level depends on verbosity/quiet
  * used for implementation details and debugging/tracing

* `ui_log` (logger name `sos_ui`):
  * INFO by default; DEBUG if `verbosity > 1`
  * intended for user-visible messages (progress, warnings, result summary)

Both (when not listing-only) write to temp files created via `get_temp_file()` so they can be included in the archive later.

---

### Tempdir, TempFileUtil, and safety constraints

* `sys_tmp`: public writable directory (usually `/var/tmp`)
* `tmpdir`: private run workspace directory: `/var/tmp/sos.<random>`
  * created with `tempfile.mkdtemp` so name is unpredictable
  * removed at end of run

Why the two-level approach matters:

* Many final results must be moved to a predictable location (`sys_tmp`).
* Intermediate files must not be vulnerable to symlink attacks or races.
* Checksums are created inside private tmpdir and then atomically renamed to final location.

---

### Archive construction and encryption flow

`SoSComponent.setup_archive()`:

1. If `--encrypt`:
   * prompt for encryption method unless `--batch`
   * supports env var configuration via `SOSENCRYPTKEY` / `SOSENCRYPTPASS`
2. Determine archive root name:
   * default: `policy.get_archive_name()`
3. Determine archive class:
   * if compression type `auto`: `policy.get_preferred_archive()`
   * else: `TarFileArchive`
4. Instantiate archive with:
   * archive base path inside `tmpdir`
   * threads count
   * encryption opts
   * sysroot
   * manifest

This means: **archive object exists only after prework**, not during SoSComponent init.

---

### Manifest/metadata deep dive (`SoSMetadata`)

`SoSMetadata` is a tiny nested structure builder.

Key point:

* It is *not* a dict, but acts like one for JSON serialization: `json.dumps(self, default=lambda o: getattr(o, '_values', str(o)))`.

Patterns in report:

* `self.manifest.components.add_section('report')` creates a nested “report” section.
* Plugins create their own section under `report_md.plugins`.

This supports a structured manifest tree.

---

## `sos report`: `SoSReport` (plugin runtime + pipeline)

**File:** `sos/report/__init__.py`  
**Class:** `SoSReport(SoSComponent)`

### SoSReport attributes reference

These are the major fields SoSReport sets and how they are used:

| Attribute | Set in | Meaning | Used by |
|---|---:|---|---|
| `loaded_plugins` | `__init__` | list of `(name, plugin_instance)` that will run | setup/collect/postproc/reports |
| `skipped_plugins` | `__init__` | list of skipped plugins with reason | `list_plugins()` |
| `all_options` | `__init__` | flattened list of plugin option objects | `list_plugins()` |
| `env_vars` | `__init__` | set of env var names to capture | `collect_env_vars()` |
| `_args` | `__init__` | saved parsed args | legacy reference |
| `sysroot` | `__init__` | report’s sysroot decision (policy/cmdline/container) | `get_commons()`, command execution |
| `estimated_plugsizes` | `__init__` | plugin size map in estimate-only | estimate printing |
| `report_md` | `__init__` | manifest subtree for report component | manifest writing |
| `cmddir/logdir/rptdir` | `_set_directories()` | internal archive dirs | archive writing + report links |
| `devices` | `_get_hardware_devices()` | enumerated devices | plugins via commons |
| `namespaces` | `_get_namespaces()` | enumerated namespaces | plugins via commons |
| `plugin_names` | `load_plugins()` | names of discovered plugins | unknown plugin checks |
| `profiles` | `load_plugins()` | set of profiles from usable plugins | list_profiles |
| `pluglist/running_plugs` | `collect()` | progress tracking | ui_progress |
| `archive` | `prework()->setup_archive()` | archive instance | everything |

### `SoSReport.__init__` step-by-step

After `SoSComponent.__init__` finishes:

1. Initializes bookkeeping lists/sets.
2. Prints header via `print_header()` → ui_log info “sos report version”.
3. Debug mode:
   * `_set_debug()` configures exception hooks + whether to raise plugin exceptions.
4. Root check reference:
   * `self._is_root = self.policy.is_root()`
5. Manifest subtree:
   * `self.report_md = self.manifest.components.add_section('report')`
6. Directory names:
   * `_set_directories()` sets names used **inside the archive**:
     * `sos_commands`, `sos_logs`, `sos_reports`
7. Sysroot logic:
   * starts from `policy.sysroot`
   * if `--sysroot` set: cmdline wins
   * if in container and sysroot differs: policy may override
8. Validate chroot mode (`--chroot` must be in `["auto","always","never"]`).
9. Container runtime selection sanity (`_check_container_runtime()`).
10. Namespaces + devices inventories.

### `SoSReport.execute` pipeline step-by-step (with code and state transitions)

This is the “main function” of `sos report`. Understanding it means understanding:

* what **state** (`self.*`) is created/modified at each step
* what **side effects** occur (archive writes, logs, manifest fields)
* which steps can **exit early**
* which steps **must** happen in order

Below is the structure of the method (abridged, but in the same order as the real code in `sos/report/__init__.py`):

```python
def execute(self):
    try:
        self.policy.set_commons(self.get_commons())
        self.load_plugins()
        self._set_all_options()
        self._merge_preset_options()
        self._set_tunables()
        self._check_for_unknown_plugins()
        self._set_plugin_options()

        if self.opts.list_plugins:
            self.list_plugins(); raise SystemExit
        if self.opts.list_profiles:
            self.list_profiles(); raise SystemExit
        if self.opts.list_presets:
            self.list_presets(); raise SystemExit
        if self.opts.add_preset:
            self.add_preset(self.opts.add_preset); raise SystemExit
        if self.opts.del_preset:
            self.del_preset(self.opts.del_preset); raise SystemExit

        if not self.verify_plugins():
            raise SystemExit

        self.batch()
        self.prework()
        self.add_manifest_data()
        self.setup()
        self.collect()
        if not self.opts.no_env_vars:
            self.collect_env_vars()
        if not self.opts.no_report:
            self.generate_reports()
        if not self.opts.no_postproc:
            self.postproc()
        self.version()
        return self.final_work()

    except ...:
        ...
```

Now, step-by-step with more detail.

#### 1) `self.policy.set_commons(self.get_commons())`

**Code:**

```python
self.policy.set_commons(self.get_commons())
```

**What `get_commons()` contains at this moment:**

* archive layout strings: `cmddir`, `logdir`, `rptdir`, `tmpdir`
* `soslog` logger (already initialized by SoSComponent)
* `policy` object itself
* `sysroot` (SoSReport’s chosen sysroot)
* `verbosity` and `cmdlineopts` (`self.opts`)
* discovered `devices` and `namespaces` (from SoSReport.__init__)

**Why this is done first:** the policy banner text (`policy.get_msg()` used in `batch()`) and policy decisions (archive name, hash, presets) may need access to current options and tmpdir.

**State changes:**

* `policy.commons` is set (Policy just stores this dict).

#### 2) `self.load_plugins()`: discovery + instantiate plugin objects

**Code:**

```python
self.load_plugins()
```

**High-level effect:**

* Finds plugin modules under `sos/report/plugins/`
* Imports candidate classes for each module
* Chooses the policy-appropriate tagging subclass
* Applies enable/skip/only/profile/default rules
* Instantiates plugin objects with `plugin_class(self.get_commons())`
* Populates:
  * `self.loaded_plugins`: list[(plugname, plugin_instance)]
  * `self.skipped_plugins`: list[(plugname, plugin_instance, reason)]
  * `self.plugin_names`: list[str] (names of discovered modules)
  * `self.profiles`: set[str] (profiles available from usable plugins)

**State changes:**

* `loaded_plugins`, `skipped_plugins`, `plugin_names`, `profiles`

**Side effects:**

* Plugin constructors execute (so any constructor logic runs here).
* Any plugin `check_enabled()`/`default_enabled()` calls happen here because `SoSReport` calls those methods while deciding whether to load/skip.

#### 3) `_set_all_options()` (global `--alloptions` flag)

**Code:**

```python
self._set_all_options()
```

**Semantics:**

If `--alloptions` is set, then for every loaded plugin:

* iterate plugin’s option objects (`plug.options.values()`)
* if the option type includes `bool`, force `opt.value = True`

This is why this step must run **after** plugin instantiation (options exist only on plugin objects) and **before** plugin setup/collect (so plugins see the right values).

**State changes:**

* plugin option values (mutates per-plugin `options` entries)

#### 4) `_merge_preset_options()` (logging, not merging)

Despite the name, this function is about logging the preset and effective options after preset application already happened in `SoSComponent.load_options()`.

**Code:**

```python
self._merge_preset_options()
```

**What it logs (soslog):**

* the command line invocation (`sos ...`)
* preset name and the preset’s args (`self.preset.opts.to_args()`)
* effective merged options (`self.opts.to_args()`)

This is a traceability step: a reader can reconstruct “why did sos behave like this”.

#### 5) `_set_tunables()` (`--plugopts`)

**Code:**

```python
self._set_tunables()
```

**What it does:**

* Parses each `--plugopts` entry like: `plugname.option=value`
* Normalizes truthy strings to booleans and tries int conversions
* Validates each option exists on the target plugin
* Applies the value via `plug.options[opt].set_value(...)`

**State changes:**

* plugin option values (again, but more targeted than `--alloptions`)

**Failure behavior:**

* If user sets an option that does not exist for a plugin, sos logs an error and exits.

#### 6) `_check_for_unknown_plugins()`

**Code:**

```python
self._check_for_unknown_plugins()
```

Ensures:

* every plugin referenced in `--only-plugins` or `--enable-plugins` exists (fatal if not)
* every plugin referenced in `--skip-plugins` exists (warning if not)

This uses `self.plugin_names` built in `load_plugins()`.

This stage exists to fail fast before doing expensive work.

#### 7) `_set_plugin_options()` (prepare listing output)

**Code:**

```python
self._set_plugin_options()
```

This collects each plugin’s option objects into `self.all_options` so that `list_plugins()` can display them.

**State changes:**

* `self.all_options` populated

#### 8) Listing / preset management early exits

These are “read-only” operations that should not proceed to archive generation:

```python
if self.opts.list_plugins:
    self.list_plugins(); raise SystemExit
...
if self.opts.add_preset:
    self.add_preset(...); raise SystemExit
```

**Key point:** This is why `SoSComponent` sets up logging and tmpdir even for listing operations, but `check_listing_options()` is used to avoid file logging handlers for those (so listing doesn’t create archive/log tempfiles unnecessarily).

#### 9) `verify_plugins()`

```python
if not self.verify_plugins():
    raise SystemExit
```

For report, this is essentially “did any plugin end up enabled”.

If zero enabled, `sos report` exits (after logging a message).

#### 10) `batch()` (policy banner + interactive gate)

**Code:**

```python
self.batch()
```

* Builds message from `policy.get_msg()`, which uses:
  * `policy.os_release_name`
  * vendor URLs/text
  * and (critically) `policy.commons['cmdlineopts'].allow_system_changes` to indicate whether system-modifying commands are allowed.
* If `--estimate-only`, it additionally enforces:
  * `threads=1`
  * `build=True`
  * `no_postproc=True`
  * `clean=False`
  * `upload=False`
  and prints that overrides were applied.
* If not `--batch`, waits for ENTER.

**State changes:**

* In estimate-only mode, this step mutates `self.opts.*` (threads/build/no_postproc/clean/upload).

#### 11) `prework()` (archive creation + internal dirs)

**Code:**

```python
self.prework()
```

Inside `prework()`:

1. `self.policy.pre_work()` hook
2. `"Setting up archive ..."` printed to ui
3. `self.setup_archive()` creates `self.archive`
4. `_make_archive_paths()` creates:
   * `sos_commands/`
   * `sos_logs/`
   * `sos_reports/`

**State changes:**

* `self.archive` becomes non-None and points to an archive root inside `self.tmpdir`.

**Failure behavior:**

* fatal filesystem errors (ENOSPC, EROFS) abort with direct printing (logging may not be reliable).

#### 12) `add_manifest_data()` (global report manifest fields)

**Code:**

```python
self.add_manifest_data()
```

Adds to `self.report_md` (manifest subtree):

* `sysroot`
* `preset`
* `profiles` list
* `priority` section (ionice class + niceness)
* `devices` section containing enumerated device maps
* `enabled_plugins` / `disabled_plugins`
* creates `plugins` section container for plugin-specific sections

This is important because plugin setup/collect will attach under `report_md.plugins`.

#### 13) `setup()` (plugin setup stage)

**Code:**

```python
self.setup()
```

Per plugin:

* create plugin manifest section under `report_md.plugins.<plugname>`
* `plug.set_plugin_manifest(...)`
* add setup timing fields
* inject archive: `plug.archive = self.archive`
* `plug.add_default_collections()`
* `plug.setup()`
* merge plugin env var requests into `self.env_vars`
* optional verify setup: `plug.setup_verify()` if `--verify`

This stage defines *what will be collected* (commands/files) before anything runs.

#### 14) `collect()` (plugin collect stage, threaded)

**Code:**

```python
self.collect()
```

This is where collections actually happen.

Key mechanics:

* Builds `self.pluglist` as a numbered list of plugins for progress display.
* Uses `ThreadPoolExecutor(self.opts.threads)` to map `_collect_plugin()` across plugins.
* `_collect_plugin()` uses a **nested single-thread executor** to enforce plugin timeouts:

  ```python
  with ThreadPoolExecutor(1) as pool:
      t = pool.submit(self.collect_plugin, plugin)
      t.result(timeout=_plug.timeout or None)
  ```

* `collect_plugin()`:
  * updates `running_plugs`
  * prints progress line
  * calls `plug.collect_plugin()` (plugin’s core collection routine)
  * removes it from progress tracking lists
  * prints finishing status

**State changes:**

* plugin manifests get start/end/run_time fields (per plugin)
* plugin object lists (executed_commands/copied_files/etc) populate (used later by generate_reports)

#### 15) `collect_env_vars()` (optional)

```python
if not self.opts.no_env_vars:
    self.collect_env_vars()
```

Writes a file `environment` in the archive containing selected env vars.

#### 16) `generate_reports()` (optional)

```python
if not self.opts.no_report:
    self.generate_reports()
```

Builds a `Report()` structure and renders:
* `sos_reports/sos.txt`
* `sos_reports/sos.json`
* `sos_reports/sos.html`

Links point to command output files under `sos_commands` and copied file paths.

#### 17) `postproc()` (optional)

```python
if not self.opts.no_postproc:
    self.postproc()
```

Calls `plug.postproc()` for each plugin if that plugin’s postproc option is enabled.

Adds timing fields into plugin manifest.

#### 18) `version()`

```python
self.version()
```

Writes `version.txt` in the archive root, currently containing at least `sos report: <version>`.

#### 19) `final_work()` (packaging, checksums, upload, cleanup)

```python
return self.final_work()
```

This stage does:

* manifest tag summary
* optional cleaner obfuscation in-place
* adds sos logs into archive
* writes final manifest
* obfuscates upload credentials in logs/manifest/collected outputs
* finalizes archive (unless `--build`)
* computes checksum (policy-defined algorithm)
* atomic rename/move to final destination under `sys_tmp`
* optionally run upload component
* cleanup tmpdir

This stage is the one that publishes final user-visible results.


---

## Plugin discovery & selection mechanics (and how policy participates)

The key cooperating pieces are:

* `ImporterHelper` (from `sos.utilities`) → lists modules under `sos.report.plugins`
* `sos.report.plugins.import_plugin` → imports plugin module and returns candidate classes
* `policy.valid_subclasses` → which distro tagging base classes are allowed
* `policy.match_plugin()` → choose among candidate classes
* `policy.validate_plugin()` → accept/reject plugin class for this policy
* runtime selection rules:
  * `check_enabled()`
  * `default_enabled()`
  * profiles
  * enable/skip/only options

The reason this is structured this way:

* plugin modules can contain multiple tagged subclasses (e.g. Debian vs Ubuntu variants)
* policy decides which tagged subclass “wins”
* policy enforces which families of tags are allowed

---

## Policy deep dive: how sos picks a policy and what policy controls

**File:** `sos/policies/__init__.py`

### How policy is chosen: `sos.policies.load()`

`load()`:

1. Uses `ImporterHelper(sos.policies.distros)` to find distro policy modules.
2. For each module, it imports policy classes via `import_policy(module)` which imports `sos.policies.distros.<name>`.
3. For each policy class, runs `policy.check(...)`.
4. The first policy whose `check()` returns True is instantiated and cached.

If no policy matches, it falls back to `GenericLinuxPolicy()`.

### What Policy controls that matters to `sos report`

Even in the code shown, policy controls:

* `get_preferred_archive()` (archive class selection)
* `get_archive_name()` (naming pattern: legacy/friendly/custom)
* `get_tmp_dir()` (tmp dir defaults)
* `get_preferred_hash_name()` (checksum algorithm)
* `validate_plugin()` and `match_plugin()` (plugin class selection)
* presets registry + probing
* banner message and `display_results()`

---

## Design patterns (precise mapping to code)

* **Command pattern**: each component is an executable object with `execute()`.
* **Template method**: SoSComponent provides the standardized initialization template and expects subclass `execute()`.
* **Strategy/Adapter**: Policy layer selects behavior by distro/platform.
* **Factory**: policy returns preferred archive class; report instantiates it.
* **Plugin architecture + discovery**: report loads plugins dynamically and runs their lifecycle.
* **Dependency injection**: “commons” dict passed into plugin constructors.

---

## How to add a new sos subcommand (component) after reading this

### Minimal recipe (the reliable path)

1. Create a module/package: `sos/mycomponent/__init__.py`
2. Implement `class SoSMyComponent(SoSComponent)`:
   * set `desc`
   * set `root_required` if needed
   * set `arg_defaults`
   * implement `add_parser_options(parser)`
   * implement `execute()`
3. Register it in `sos/__init__.py` inside `SoS.__init__`:
   * import your module
   * add to `self._components`

### Critical rules for “it behaves like a real sos command”

* Put defaults in `arg_defaults` so config/preset merge works.
* Read effective options from `self.opts`, not from `self.args`.
* If you need tmpdir/logging, keep `configure_logging=True`.
* If you need distro behavior, keep `load_policy=True`.
* If you create archives, use `setup_archive()` and respect policy naming/paths.
