# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

import os
import logging
import errno
from datetime import datetime

from sos import __version__
from sos import _sos as _
from sos.component import SoSComponent
from sos.utilities import ImporterHelper, import_module

# file system errors that should terminate a run
fatal_fs_errors = (errno.ENOSPC, errno.EROFS)


class SoSVmcoreReport(SoSComponent):
    """Analyze a vmcore with drgn and collect results into an archive"""

    desc = "Collect vmcore diagnostics using drgn"
    root_required = False

    # Component-specific defaults (merged over SoSComponent._arg_defaults)
    arg_defaults = {
        # vmcore-specific
        "vmcore": "",
        "vmlinux": None,
        "debuginfo_dirs": [],
        # legacy alias for only-plugins
        "plugins": [],
        # selection controls (mirror sos report semantics)
        "enable_plugins": [],
        "skip_plugins": [],
        "only_plugins": [],
        "experimental": False,
        # archive behaviour
        "build": False,
        # optional name/label to distinguish outputs
        "label": "",
    }

    @classmethod
    def add_parser_options(cls, parser):
        grp = parser.add_argument_group(
            "VMCore Report Options",
            "Options specific to analyzing a vmcore with drgn",
        )
        grp.add_argument(
            "--vmcore",
            required=True,
            help="Path to vmcore (e.g. /var/crash/.../vmcore)",
        )
        grp.add_argument(
            "--vmlinux",
            default=None,
            help="Path to vmlinux (uncompressed kernel image with debuginfo)",
        )
        grp.add_argument(
            "--debuginfo-dir",
            dest="debuginfo_dirs",
            action="extend",
            type=str,
            default=[],
            help="Extra directories to search for debug symbols (may be given multiple times)",
        )
        grp.add_argument(
            "--plugins",
            dest="plugins",
            action="extend",
            type=str,
            default=[],
            help="Limit execution to the listed drgn plugins (comma or multiple --plugins allowed)",
        )
        grp.add_argument(
            "-e", "--enable-plugins",
            action="extend",
            dest="enable_plugins",
            type=str,
            default=[],
            help="enable these plugins",
        )
        grp.add_argument(
            "-o", "--only-plugins",
            action="extend",
            dest="only_plugins",
            type=str,
            default=[],
            help="enable these plugins only",
        )
        grp.add_argument(
            "-n", "--skip-plugins",
            action="extend",
            dest="skip_plugins",
            type=str,
            default=[],
            help="disable these plugins",
        )
        grp.add_argument(
            "--experimental",
            action="store_true",
            dest="experimental",
            default=False,
            help="enable experimental plugins",
        )
        grp.add_argument(
            "--build",
            action="store_true",
            dest="build",
            default=False,
            help="Preserve the directory and do not package results",
        )
        grp.add_argument(
            "--label",
            action="store",
            dest="label",
            default="",
            help="Specify an additional report label",
        )

    def __init__(self, parser, args, cmdline):
        super().__init__(parser, args, cmdline)
        self._drgn_prog = None
        self._loaded_plugins = []
        self._skipped_plugins = []
        self._plugins_pkg = None
        self._archive_initialized = False

    def print_header(self):
        self.ui_log.info(f"\n{_(f'sos vmcore-report (version {__version__})')}\n")

    def _validate_inputs(self):
        # vmcore path
        if not self.opts.vmcore:
            raise Exception("--vmcore is required")
        if not os.path.exists(self.opts.vmcore):
            raise Exception(f"vmcore not found: {self.opts.vmcore}")
        # vmlinux is optional for MVP; if provided, must exist
        if self.opts.vmlinux and not os.path.exists(self.opts.vmlinux):
            raise Exception(f"vmlinux not found: {self.opts.vmlinux}")
        for d in self.opts.debuginfo_dirs:
            if not os.path.isdir(d):
                self.soslog.warning(f"Ignoring non-directory --debuginfo-dir: {d}")

    def _archive_name(self):
        # Derive an archive name similar to report but with a distinct prefix
        # Attempt to use policy naming and adjust prefix
        try:
            name = self.policy.get_archive_name()
        except Exception:
            # Fallback generic
            name = "sosreport-vmcore"
        # Replace common prefix if present
        name = name.replace("sosreport-", "sosvmcore-")
        if self.opts.label:
            # Insert label prior to timestamp-ish suffix if possible
            # Else simply append
            if not name.endswith(self.opts.label):
                name = f"{name}-{self.opts.label}"
        return name

    def _prepare_archive(self):
        if self._archive_initialized:
            return
        self.ui_log.info(_(" Setting up archive ..."))
        try:
            self.setup_archive(name=self._archive_name())
            # Create standard dirs for consistency
            self.archive.makedirs("sos_logs", 0o755)
            self.archive.makedirs("sos_reports", 0o755)
            self.archive.makedirs("plugins", 0o755)
            self._archive_initialized = True
        except OSError as e:
            if e.errno in fatal_fs_errors:
                print("")
                print(f" {e.strerror} while setting up archive")
                print("")
            else:
                print(f"Error setting up archive: {e}")
                raise
        except Exception as e:
            self.ui_log.error("")
            self.ui_log.error(" Unexpected exception setting up archive:")
            self.ui_log.error(e)
            raise

    def _init_drgn(self):
        # Import lazily to present clean error if missing
        try:
            from .drgn_context import build_program
        except Exception as e:
            raise Exception(f"Failed to import drgn context: {e}")
        try:
            self._drgn_prog = build_program(
                vmcore=self.opts.vmcore,
                vmlinux=self.opts.vmlinux,
                extra_debug_paths=self.opts.debuginfo_dirs or [],
                logger=self.soslog,
            )
        except ModuleNotFoundError:
            raise Exception(
                "drgn is not installed. Install python3-drgn (or drgn) to use sos vmcore-report."
            )
        except Exception as e:
            raise Exception(f"Failed to initialize drgn program: {e}")

    def _discover_plugins(self):
        # Load plugin package and discover modules
        import sos.vmcore_report.plugins as pkg
        from .plugin import DrgnPluginBase

        self._plugins_pkg = pkg
        helper = ImporterHelper(pkg)
        modules = helper.get_modules()

        # Enforce strict organization: sys/ is owned by vmcore_sysfs emitters.
        # Keep sched_debug and other non-emitter sys writers out of the plugin set.
        # (sched_debug output is now expected to be produced by sys emitters.)
        modules = [m for m in modules if m not in ("sched_debug",)]

        # Selection semantics similar to sos report
        only_set = set([p.split(".")[0] for p in (self.opts.only_plugins or [])])
        # Support legacy/alias --plugins as 'only'
        legacy_only = set([p.split(".")[0] for p in (self.opts.plugins or [])])
        only_set |= legacy_only

        skip_set = set([p.split(".")[0] for p in (self.opts.skip_plugins or [])])
        enable_set = set([p.split(".")[0] for p in (self.opts.enable_plugins or [])])

        experimental_ok = bool(getattr(self.opts, "experimental", False))

        self._loaded_plugins = []
        self._skipped_plugins = []

        discovered = []

        for mod_name in modules:
            fq = f"{pkg.__name__}.{mod_name}"
            try:
                classes = import_module(fq, superclasses=(DrgnPluginBase,))
            except Exception as e:
                self.soslog.warning(f"plugin {mod_name} does not import: {e}")
                continue
            if not classes:
                continue
            # Each module should export exactly one DrgnPluginBase subclass
            cls = classes[0]
            pname = cls.name()
            discovered.append(pname)

            # Only filter
            if only_set and pname not in only_set:
                self._skipped_plugins.append((pname, "not specified"))
                continue

            # Skip filter
            if pname in skip_set:
                self._skipped_plugins.append((pname, "skipped"))
                continue

            # Experimental gating
            if getattr(cls, "experimental", False) and not experimental_ok:
                # allow explicit enable/only to override
                if pname not in enable_set and pname not in only_set:
                    self._skipped_plugins.append((pname, "experimental"))
                    continue

            # Optional (default_disabled) gating
            if not getattr(cls, "default_enabled", True):
                if pname not in enable_set and pname not in only_set:
                    self._skipped_plugins.append((pname, "optional"))
                    continue

            self._loaded_plugins.append(cls)

        # warn unknown names in only/skip/enable sets
        unknown = (only_set | skip_set | enable_set) - set(discovered)
        for want in sorted(unknown):
            self.soslog.warning(f"Requested plugin '{want}' not found")

    def _run_plugins(self):
        self.ui_log.info(_(" Running drgn plugins. Please wait ..."))
        self.ui_log.info("")

        base_outdir = "plugins"
        for cls in self._loaded_plugins:
            pname = cls.name()
            status_line = f"  Starting {pname:<20}"
            self.ui_progress(status_line)
            try:
                plugin = cls(
                    outdir=os.path.join(base_outdir, pname),
                    logger=self.soslog,
                    archive=self.archive,
                )
                # runtime activation check
                try:
                    if not plugin.check_enabled(self._drgn_prog):
                        self._skipped_plugins.append((pname, "inactive"))
                        continue
                except Exception:
                    self._skipped_plugins.append((pname, "inactive"))
                    continue
                plugin.setup(self._drgn_prog)
                plugin.collect(self._drgn_prog)
            except KeyboardInterrupt:
                raise
            except OSError as e:
                if e.errno in fatal_fs_errors:
                    self.ui_log.error(f"\n {e.strerror} while running plugin {pname}")
                    self.ui_log.error(
                        f" Data collected still available at {self.tmpdir}\n"
                    )
                    os._exit(1)
                self.soslog.exception(f"Plugin {pname} failed with OSError")
            except Exception as e:
                self.soslog.exception(f"Plugin {pname} failed: {e}")

        self.ui_log.info("")
        self.ui_progress("  Finished running plugins")

    def _add_logs(self):
        # mirror report's behavior and include sos logs
        if getattr(self, "sos_log_file", None):
            self.archive.add_file(self.sos_log_file, dest=os.path.join("sos_logs", "sos.log"))
        if getattr(self, "sos_ui_log_file", None):
            self.archive.add_file(self.sos_ui_log_file, dest=os.path.join("sos_logs", "ui.log"))

    def _create_checksum(self, archive, hash_name):
        if not archive:
            return False

        import hashlib
        try:
            hash_size = 1024**2
            digest = hashlib.new(hash_name)
            with open(archive, "rb") as archive_fp:
                while True:
                    hashdata = archive_fp.read(hash_size)
                    if not hashdata:
                        break
                    digest.update(hashdata)
        except Exception:
            self.soslog.exception("Error generating checksum")
            return None
        return digest.hexdigest()

    def _write_checksum(self, archive, hash_name, checksum):
        try:
            with open(archive + "." + hash_name, "w", encoding="utf-8") as fp:
                if checksum:
                    fp.write(checksum + "\n")
        except Exception:
            self.soslog.exception("Error writing checksum file")

    def _finalize(self):
        # Add manifest and logs
        try:
            self.archive.add_final_manifest_data(self.opts.compression_type)
        except Exception as e:
            # do not abort packaging for manifest issues
            self.soslog.warning(f"Failed writing manifest: {e}")
        self._add_logs()

        # attach ui log to stdout even in --quiet, mirroring report
        if self.opts.quiet:
            self.add_ui_log_to_stdout()

        archive = None
        directory = None
        checksum = None

        # Package up and compress the results
        if not self.opts.build:
            old_umask = os.umask(0o077)
            if not self.opts.quiet:
                print(_("Creating compressed archive..."))
            try:
                archive = self.archive.finalize(self.opts.compression_type)
            except OSError as e:
                print("")
                print(
                    _(
                        f" {e.strerror} while finalizing archive "
                        f"{self.archive.get_archive_path()}"
                    )
                )
                print("")
                if e.errno in fatal_fs_errors:
                    self._exit(1)
            except Exception:
                if self.opts.debug:
                    raise
                # continue to cleanup path
                archive = None
            finally:
                os.umask(old_umask)
        else:
            # move the archive root out of the private tmp directory.
            directory = self.archive.get_archive_path()
            dir_name = os.path.basename(directory)
            try:
                final_dir = os.path.join(self.sys_tmp, dir_name)
                os.rename(directory, final_dir)
                directory = final_dir
            except OSError:
                print(_(f"Error moving directory: {directory}"))
                return False

        if not self.opts.build:
            if archive:
                try:
                    hash_name = self.policy.get_preferred_hash_name()
                    checksum = self._create_checksum(archive, hash_name)
                except Exception:
                    print(
                        _(
                            "Error generating archive checksum after "
                            "archive creation.\n"
                        )
                    )
                    checksum = None
                try:
                    if checksum:
                        self._write_checksum(archive, hash_name, checksum)
                except Exception:
                    print(_(f"Error writing checksum for file: {archive}"))

                base_archive = os.path.basename(archive)
                final_name = os.path.join(self.sys_tmp, base_archive)
                archivestat = os.stat(archive)

                archive_hash = archive + "." + hash_name
                final_hash = final_name + "." + hash_name

                try:
                    os.rename(archive, final_name)
                    archive = final_name
                except OSError:
                    print(_(f"Error moving archive file: {archive}"))
                    return False

                try:
                    os.rename(archive_hash, final_hash)
                except OSError:
                    print(_(f"Error moving checksum file: {archive_hash}"))

                self.policy.display_results(
                    archive, directory, checksum, archivestat
                )
            else:
                print("Creating archive tarball failed.")
        else:
            self.policy.display_results(archive, directory, checksum)

        # clean up
        logging.shutdown()
        if self.tempfile_util:
            self.tempfile_util.clean()
        if self.tmpdir and os.path.isdir(self.tmpdir):
            from shutil import rmtree
            rmtree(self.tmpdir)
        return True

    def ui_progress(self, status_line):
        if self.opts.verbosity == 0 and not self.opts.batch:
            status_line = f"\r{status_line.ljust(90)}"
        else:
            status_line = f"{status_line}\n"
        if not self.opts.quiet:
            import sys as _sys
            _sys.stdout.write(status_line)
            _sys.stdout.flush()

    def execute(self):
        try:
            self.print_header()
            # ensure policy common bits are available
            self.policy.set_commons({
                'tmpdir': self.tmpdir,
                'verbosity': self.opts.verbosity,
                'cmdlineopts': self.opts,
            })
            self._validate_inputs()
            self._prepare_archive()
            self._init_drgn()
            self._discover_plugins()
            if not self._loaded_plugins:
                self.ui_log.warning("No vmcore-report plugins enabled")
            self._run_plugins()
            return self._finalize()
        except KeyboardInterrupt:
            self.ui_log.error("\nExiting on user cancel")
            self.cleanup()
            self._exit(130)
        except SystemExit as e:
            if not os.getenv('SOS_TEST_LOGS', None) == 'keep':
                self.cleanup()
            raise
        except Exception as e:
            self.ui_log.error(f"Error: {e}")
            if self.opts.debug:
                raise
            if not os.getenv('SOS_TEST_LOGS', None) == 'keep':
                self.cleanup()
        self._exit(1)
        return False
