# This file is part of the sos project: https://github.com/sosreport/sos
#
# Writer helpers for vmcore-report emitters.
#
# Emitters should only produce text; these helpers take (path, text)
# pairs and write them into the active Archive using add_string(),
# ensuring archive-relative paths like "proc/..." or "sys/..." are
# created with correct leading directories.

from typing import Iterable, Tuple


def write_file(archive, path: str, text: str):
    """Write a single file to the archive at the relative 'path'."""
    if not isinstance(text, str):
        text = str(text)
    # Archive.add_string handles creating intermediate directories via
    # check_path()/_make_leading_paths(); 'path' should be relative.
    archive.add_string(text, path, mode="w")


def write_files(archive, files: Iterable[Tuple[str, str]]):
    """Write multiple (path, text) entries into the archive."""
    for path, text in files:
        write_file(archive, path, text)
