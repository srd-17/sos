# This file is part of the sos project: https://github.com/sosreport/sos
#
# Base classes and helpers for drgn-based vmcore-report plugins.

import json
import os


class DrgnPluginBase:
    """Base class for vmcore-report plugins that use drgn.

    Subclasses should implement:
      - collect(self, prog): perform all data collection and write outputs
    Optionally override:
      - setup(self, prog): perform any pre-collection initialization
      - name()/get_description(): metadata for the plugin
    """

    plugin_name = None
    description = "No description provided"
    # Whether this plugin should run by default (can be overridden by CLI)
    default_enabled = True
    # Mark plugin as experimental; requires --experimental to enable by default
    experimental = False

    def __init__(self, outdir, logger, archive):
        """
        Args:
          outdir: Path inside the archive where this plugin writes output,
                  e.g. 'plugins/kernel_info'
          logger: sos logger instance
          archive: sos Archive instance for writing files
        """
        self.outdir = outdir.strip("/")

        # Normalize to 'plugins/<name>' if just name was passed
        if not self.outdir.startswith("plugins/"):
            self.outdir = os.path.join("plugins", self.outdir)

        self.logger = logger
        self.archive = archive

    # ----- Metadata helpers -----

    @classmethod
    def name(cls):
        if cls.plugin_name:
            return cls.plugin_name
        # Convert CamelCaseClass to snake_case for default name
        import re
        s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", cls.__name__)
        return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    @classmethod
    def get_description(cls):
        return getattr(cls, "description", "")

    # ----- Lifecycle hooks -----

    def setup(self, prog):
        """Optional hook before collect(). Default: no-op."""
        return

    def check_enabled(self, prog):
        """Return True if plugin should run in this vmcore context."""
        return True

    def collect(self, prog):
        """Perform plugin data collection using drgn Program 'prog'."""
        raise NotImplementedError

    # ----- Write helpers -----

    def _join(self, relpath):
        return os.path.join(self.outdir, relpath.lstrip("/"))

    def write_text(self, relpath, content, mode="w"):
        """Write textual content into the plugin's output directory."""
        dest = self._join(relpath)
        if not isinstance(content, str):
            content = str(content)
        self.archive.add_string(content, dest, mode=mode)
        self.logger.debug(f"[{self.name()}] wrote {dest}")

    def write_json(self, relpath, obj, mode="w"):
        """Write JSON content into the plugin's output directory."""
        dest = self._join(relpath)
        txt = json.dumps(obj, indent=2, default=str)
        self.archive.add_string(txt, dest, mode=mode)
        self.logger.debug(f"[{self.name()}] wrote {dest}")

    def write_lines(self, relpath, lines, mode="w"):
        """Write a list of lines to a file."""
        if not isinstance(lines, (list, tuple)):
            lines = [str(lines)]
        text = "".join(
            (ln if ln.endswith("\n") else f"{ln}\n") for ln in lines
        )
        self.write_text(relpath, text, mode=mode)
