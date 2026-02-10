# This file is part of the sos project: https://github.com/sosreport/sos
#
# Helper to initialize a drgn Program for vmcore analysis (drgn-only).
#
# Loading strategy (best-effort, no external dependencies):
#   - Program()
#   - set_core_dump(<vmcore path> or fd via sudohelper on PermissionError)
#   - Load debuginfo:
#       * Prefer explicit files from --vmlinux and --debuginfo-dir (vmlinux, *.ko.debug)
#       * Else try load_default_debug_info() if available
#       * Else try load_debug_info(default=True) or load_debug_info(main=True)
#   - Never raise on missing debuginfo; plugins handle gaps and emit WARN stubs.

import os
import glob
from typing import List, Optional


def _collect_debug_files(extra_debug_paths: List[str]) -> List[str]:
    files: List[str] = []
    for d in extra_debug_paths:
        if not d or not os.path.isdir(d):
            continue
        # Common names/locations:
        #  - vmlinux in a directory
        #  - *.ko.debug modules (flat or nested)
        vmlinux_path = os.path.join(d, "vmlinux")
        if os.path.isfile(vmlinux_path):
            files.append(vmlinux_path)
        files.extend(glob.glob(os.path.join(d, "*.ko.debug")))
        files.extend(glob.glob(os.path.join(d, "**", "*.ko.debug"), recursive=True))
    # De-dup while preserving order
    seen = set()
    uniq = []
    for f in files:
        if f not in seen:
            uniq.append(f)
            seen.add(f)
    return uniq


def build_program(vmcore: str,
                  vmlinux: Optional[str] = None,
                  extra_debug_paths: Optional[List[str]] = None,
                  logger=None):
    """
    Initialize and return a drgn.Program for a kernel vmcore.

    Parameters:
      vmcore: Path to the vmcore file (required)
      vmlinux: Optional path to vmlinux; used for debug info loading
      extra_debug_paths: Extra directories to search for symbol/debug info
      logger: Optional logger for debug/info messages

    Returns:
      drgn.Program instance configured to read the vmcore.

    Raises:
      ModuleNotFoundError if drgn is not installed.
      Exception for other initialization errors (e.g., cannot open vmcore).
    """
    try:
        import drgn
    except Exception as e:
        raise ModuleNotFoundError("drgn is not installed") from e

    # Create Program and set the core dump (path or fd). Do NOT pass vmlinux
    # into set_core_dump(); newer drgn accepts only 1 argument here.
    prog = drgn.Program()
    try:
        prog.set_core_dump(vmcore)
    except PermissionError:
        # Fallback via sudohelper for protected paths (e.g. /proc/kcore)
        try:
            from drgn.internal.sudohelper import open_via_sudo  # type: ignore
            prog.set_core_dump(open_via_sudo(vmcore, os.O_RDONLY))
        except Exception as e:
            raise Exception(f"Failed to open vmcore with drgn: {e}") from e
    except Exception as e:
        raise Exception(f"Failed to open vmcore with drgn: {e}") from e

    # Build explicit debuginfo file list from inputs
    extra_debug_paths = extra_debug_paths or []
    debug_file_list: List[str] = []
    if vmlinux and os.path.isfile(vmlinux):
        debug_file_list.append(vmlinux)
    debug_file_list.extend(_collect_debug_files(extra_debug_paths))

    # Helper: attempt a callable and return True if it succeeded
    def _try(call, *args, **kwargs):
        try:
            call(*args, **kwargs)
            return True
        except Exception as err:  # pylint: disable=broad-except
            if logger:
                logger.debug(f"debuginfo load attempt failed: {err}")
            return False

    # Attempt to load debuginfo (best-effort, drgn-only)
    # Priority: explicit files -> default search -> main-only fallback.
    loaded = False
    if debug_file_list:
        loaded = _try(prog.load_debug_info, debug_file_list)

    if not loaded:
        # Try load_default_debug_info if present
        if hasattr(prog, "load_default_debug_info"):
            loaded = _try(prog.load_default_debug_info)

    if not loaded:
        # Try default=True or main=True fallbacks (version-dependent)
        # Note: These kwargs may not exist on some drgn versions; guard with try.
        loaded = _try(prog.load_debug_info, default=True) or _try(prog.load_debug_info, main=True)

    # Missing debuginfo is non-fatal; plugins should handle gaps gracefully.
    return prog
