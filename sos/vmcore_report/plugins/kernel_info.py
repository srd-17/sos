# This file is part of the sos project: https://github.com/sosreport/sos
#
# Example drgn-based plugin for sos vmcore-report:
#  - Collects kernel utsname fields (sysname, nodename, release, version, ...)
#  - Attempts to read kernel taint flags (if drgn helpers are available)
#  - Attempts to read linux_banner
#
# Output is written under plugins/kernel_info/ in the archive.

import json

from sos.vmcore_report.plugin import DrgnPluginBase


class KernelInfoPlugin(DrgnPluginBase):
    plugin_name = "kernel_info"
    description = "Kernel basics (utsname, banner, taints)"

    def _read_uts(self, prog):
        uts = {}
        try:
            # init_uts_ns.name is struct new_utsname with char arrays
            name = prog["init_uts_ns"].name

            def to_text(b):
                try:
                    return b.decode("utf-8", "ignore")
                except Exception:
                    return str(b)

            def fld(val):
                # string_() may return bytes or str depending on drgn/version
                s = val.string_()
                if isinstance(s, (bytes, bytearray)):
                    s = to_text(s)
                return s.strip()

            uts = {
                "sysname": fld(name.sysname),
                "nodename": fld(name.nodename),
                "release": fld(name.release),
                "version": fld(name.version),
                "machine": fld(name.machine),
                "domainname": fld(name.domainname),
            }
        except Exception as e:
            self.logger.debug(f"[{self.name()}] failed reading init_uts_ns: {e}")
        return uts

    def _read_banner(self, prog):
        # linux_banner is a char[] string on many kernels
        try:
            import drgn as _drgn  # optional, used for cast convenience
            banner_obj = prog["linux_banner"]
            # Cast array to char * to use string_()
            b = _drgn.cast("char *", banner_obj).string_()
            if isinstance(b, (bytes, bytearray)):
                b = b.decode("utf-8", "ignore")
            return b.strip()
        except Exception as e:
            self.logger.debug(f"[{self.name()}] linux_banner unavailable: {e}")
            return None

    def _read_taints(self, prog):
        # Prefer drgn.helpers.linux if available
        try:
            from drgn.helpers.linux import kernel_taint_flags
            flags = list(kernel_taint_flags(prog))
            return flags
        except Exception as e:
            self.logger.debug(f"[{self.name()}] kernel taints unavailable: {e}")
            return None

    def collect(self, prog):
        uts = self._read_uts(prog)
        banner = self._read_banner(prog)
        taints = self._read_taints(prog)

        # Write discrete outputs
        if uts:
            self.write_json("uts.json", uts)
            # summary text
            lines = [
                f"sysname:    {uts.get('sysname', '')}",
                f"nodename:   {uts.get('nodename', '')}",
                f"release:    {uts.get('release', '')}",
                f"version:    {uts.get('version', '')}",
                f"machine:    {uts.get('machine', '')}",
                f"domainname: {uts.get('domainname', '')}",
            ]
            self.write_lines("uts.txt", [ln + "\n" for ln in lines])

        if banner:
            self.write_text("banner.txt", banner + "\n")

        if taints is not None:
            # Normalize to list of strings
            if not isinstance(taints, (list, tuple)):
                try:
                    taints = list(taints)
                except Exception:
                    taints = [str(taints)]
            self.write_json("taints.json", taints)

        # Also create a combined JSON for convenience
        combined = {
            "uts": uts or {},
            "banner": banner,
            "taints": taints,
        }
        self.write_text("kernel_info.json", json.dumps(combined, indent=2))
