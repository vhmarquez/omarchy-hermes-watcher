"""Shared Linux procfs parsing helpers."""

from __future__ import annotations


def process_start_ticks(stat_text: str) -> str:
    """Return field 22 from /proc/<pid>/stat without splitting comm."""
    comm_end = stat_text.rfind(")")
    comm_start = stat_text.find("(")
    if comm_start < 0 or comm_end <= comm_start:
        raise ValueError("malformed proc stat comm")
    fields_after_comm = stat_text[comm_end + 1 :].split()
    if len(fields_after_comm) <= 19:
        raise ValueError("missing proc stat starttime")
    return fields_after_comm[19]
