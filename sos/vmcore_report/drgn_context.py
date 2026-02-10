# This file is part of the sos project: https://github.com/sosreport/sos
#
# Helper to initialize a drgn Program for vmcore analysis, following the
# loading pattern used by drgn-tools:
#   - Program()
#   - set_core_dump(<vmcore or fd>)
#   - register debuginfo finders (if available)
#   - load debug info (explicit files or default/main search)

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
        #  - *.ko.debug modules
        #  - flat trees under given paths
        vmlinux_path = os.path.join(d, "vmlinux")
        if os.path.isfile(vmlinux_path):
            files.append(vmlinux_path)
        files.extend(glob.glob(os.path.join(d, "*.ko.debug")))
        # Also pick nested *.ko.debug (one level)
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
      Exception for other initialization errors.
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

    # Best-effort integration with drgn-tools (if available) to match the
    # user's environment and search logic for debuginfo.
    have_drgn_tools = False
    try:
        from drgn_tools.debuginfo import (  # type: ignore
            drgn_prog_set as register_debug_info_finders,
            get_debuginfo_config,
        )
        have_drgn_tools = True
    except Exception:
        have_drgn_tools = False

    extra_debug_paths = extra_debug_paths or []
    debug_file_list: List[str] = []

    # If the user provided a vmlinux, prefer explicit loading of that file.
    if vmlinux and os.path.isfile(vmlinux):
        debug_file_list.append(vmlinux)
    # Also add any discovered module debuginfo from extra paths.
    debug_file_list.extend(_collect_debug_files(extra_debug_paths))

    # Attempt to load debuginfo
    try:
        # If drgn-tools is installed, register its finders and try its flows.
        if have_drgn_tools:
            # Cache knobs used by drgn-tools so their hooks can consume them.
            try:
                opts = get_debuginfo_config()
                # Behave like drgn-tools CLI defaults:
                opts.enable_ctf = True
                # Always allow extraction and downloads if enabled elsewhere.
                opts.enable_extract = True
                prog.cache["drgn_tools.debuginfo.options"] = opts
                prog.cache["drgn_tools.debuginfo.vmcore_path"] = vmcore
            except Exception:
                # If config retrieval fails, continue without options.
                pass

            try:
                register_debug_info_finders(prog)
            except Exception as e:
                if logger:
                    logger.debug(f"drgn-tools: register finders failed: {e}")

            # If an explicit vmlinux was given, try loading it directly first.
            if debug_file_list:
                try:
                    prog.load_debug_info(debug_file_list)
                except Exception as e:
                    # Fall back to drgn-tools default search to resolve main/module debuginfo
                    if logger:
                        logger.debug(f"Explicit load_debug_info({len(debug_file_list)} files) failed: {e}")
                    try:
                        # If caller passed vmlinux, at least load main symbols
                        if vmlinux:
                            prog.load_debug_info(main=True)
                        else:
                            prog.load_debug_info(default=True)
                    except Exception:
                        # Swallow; missing debuginfo is non-fatal for many queries
                        pass
            else:
                # No explicit files; try default search via drgn-tools hooks
                try:
                    prog.load_debug_info(default=True)
                except Exception:
                    # Try loading just main module symbols if full default fails
                    try:
                        prog.load_debug_info(main=True)
                    except Exception:
                        pass
        else:
            # No drgn-tools; attempt explicit files first, else defaults.
            if debug_file_list:
                try:
                    prog.load_debug_info(debug_file_list)
                except Exception as e:
                    if logger:
                        logger.debug(f"Explicit load_debug_info failed: {e}")
                    # Fall back to default search (system debuginfo locations)
                    try:
                        prog.load_default_debug_info()
                    except Exception:
                        pass
            else:
                try:
                    # Try system default search
                    prog.load_default_debug_info()
                except AttributeError:
                    # Older drgn may not have load_default_debug_info(); try main/default
                    try:
                        if vmlinux:
                            prog.load_debug_info([vmlinux])
                        else:
                            prog.load_debug_info(main=True)
                    except Exception:
                        pass
                except Exception:
                    pass
    except drgn.MissingDebugInfoError:  # type: ignore
        # If DRGN raises an explicit missing debuginfo error, continue anyway.
        pass
    except Exception as e:
        # Loading debuginfo is best-effort; do not fail program creation.
        if logger:
            logger.debug(f"Non-fatal debuginfo load error: {e}")

    return prog
