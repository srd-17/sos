# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit a minimal set of /proc/<pid>/* files using drgn (best-effort).
#
# Files emitted:
#   cgroup, cpuset, limits, mountinfo, oom_adj, oom_score, oom_score_adj,
#   stack, status
#
# Design:
# - Use @emits_many with enumerator="enumerate_pids" to iterate tasks.
# - For each file, reuse a shared generator function to keep logic centralized.
# - Always return text; never write to archive from emitters.
#
# Notes:
# - Some fields are kernel-version dependent and may fail. Keep best-effort
#   behavior and prefer producing partial output over raising.

from __future__ import annotations

from typing import List, Tuple

from sos.vmcore_report.emitters.registry import emits_many

# Resource limit names and units (matches typical /proc/<pid>/limits header)
RLIMIT_NAMES: List[Tuple[str, str]] = [
    ("Max cpu time", "seconds"),
    ("Max file size", "bytes"),
    ("Max data size", "bytes"),
    ("Max stack size", "bytes"),
    ("Max core file size", "bytes"),
    ("Max resident set", "bytes"),
    ("Max processes", "processes"),
    ("Max open files", "files"),
    ("Max locked memory", "bytes"),
    ("Max address space", "bytes"),
    ("Max file locks", "locks"),
    ("Max pending signals", "signals"),
    ("Max msgqueue size", "bytes"),
    ("Max nice priority", ""),
    ("Max realtime priority", ""),
    ("Max realtime timeout", "us"),
]


def _to_str(x) -> str:
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", "ignore")
    return str(x)


def _task_comm(task) -> str:
    try:
        return _to_str(task.comm.string_()).strip()
    except Exception:
        try:
            # Sometimes comm is a char array
            return _to_str(task.comm).strip()
        except Exception:
            return "unknown"


def _task_pid(task) -> int:
    try:
        return int(task.pid.value_())
    except Exception:
        try:
            return int(task.pid)
        except Exception:
            return -1


def _task_tgid(task) -> int:
    try:
        return int(task.tgid.value_())
    except Exception:
        return -1


def _task_ppid(task) -> int:
    try:
        return int(task.parent.pid.value_())
    except Exception:
        return 0


def _task_tracer_pid(task) -> int:
    # Historical plugin used real_parent.pid; keep same as inspiration
    try:
        return int(task.real_parent.pid.value_())
    except Exception:
        return 0


def _task_state_char(prog, task) -> str:
    try:
        from drgn.helpers.linux.sched import task_state_to_char
        return str(task_state_to_char(task))
    except Exception:
        return "?"


def _task_state_val(task) -> int:
    try:
        if hasattr(task, "__state"):
            return int(task.__state.value_())
        return int(task.state.value_())
    except Exception:
        return 0


def _uid_gid_lines(task) -> Tuple[str, str]:
    # Uid: real effective saved fs
    # Gid: real effective saved fs
    try:
        cred = task.real_cred.read_()
        uid = cred.uid.val.value_()
        euid = cred.euid.val.value_()
        suid = cred.suid.val.value_()
        fsuid = cred.fsuid.val.value_()
        gid = cred.gid.val.value_()
        egid = cred.egid.val.value_()
        sgid = cred.sgid.val.value_()
        fsgid = cred.fsgid.val.value_()
        return (
            f"Uid:\t{uid}\t{euid}\t{suid}\t{fsuid}\n",
            f"Gid:\t{gid}\t{egid}\t{sgid}\t{fsgid}\n",
        )
    except Exception:
        return ("Uid:\t0\t0\t0\t0\n", "Gid:\t0\t0\t0\t0\n")


def generate_proc_pid_status(prog, task) -> str:
    # Keep this best-effort and minimal. It’s fine to extend over time.
    lines: List[str] = []
    lines.append(f"Name:\t{_task_comm(task)}\n")
    lines.append("Umask:\t0000\n")
    lines.append(f"State:\t{_task_state_char(prog, task)} ({_task_state_val(task)})\n")
    lines.append(f"Tgid:\t{_task_tgid(task)}\n")
    lines.append("Ngid:\t0\n")
    lines.append(f"Pid:\t{_task_pid(task)}\n")
    lines.append(f"PPid:\t{_task_ppid(task)}\n")
    lines.append(f"TracerPid:\t{_task_tracer_pid(task)}\n")

    uid_line, gid_line = _uid_gid_lines(task)
    lines.append(uid_line)
    lines.append(gid_line)

    # Groups are hard to recover reliably; keep placeholder
    lines.append("Groups:\n")
    # Namespace PID stubs (best-effort)
    pid = _task_pid(task)
    lines.append(f"NStgid:\t{pid}\n")
    lines.append(f"NSpid:\t{pid}\n")
    # Kernel thread indicator
    try:
        kthread = 0 if bool(task.mm) else 1
    except Exception:
        kthread = 1
    lines.append(f"Kthread:\t{kthread}\n")
    # Threads unknown without thread-group walk; keep 1
    lines.append("Threads:\t1\n")
    return "".join(lines)


def generate_proc_pid_cgroup(prog, task) -> str:
    # Best-effort: unified v2 format 0::/path
    try:
        from drgn.helpers.linux.list import list_for_each_entry
        from drgn.helpers.linux.cgroup import cgroup_path

        cgroup = task.cgroups
        # try iterate subsys list like inspiration; emit first path
        for css in list_for_each_entry(
            "struct cgroup_subsys_state",
            cgroup.subsys.address_of_(),
            "cgroup_node",
        ):
            cgrp = css.cgroup
            path = cgroup_path(cgrp)
            return f"0::{_to_str(path).strip()}\n"
    except Exception:
        pass
    return "0::/\n"


def generate_proc_pid_cpuset(prog, task) -> str:
    # Try best-effort: find cpuset controller path; else unified path; else "/"
    try:
        from drgn.helpers.linux.list import list_for_each_entry
        from drgn.helpers.linux.cgroup import cgroup_path

        cgroup = task.cgroups
        for css in list_for_each_entry(
            "struct cgroup_subsys_state",
            cgroup.subsys.address_of_(),
            "cgroup_node",
        ):
            try:
                if "cpuset" in _to_str(css.ss.name):
                    return f"{_to_str(cgroup_path(css.cgroup)).strip()}\n"
            except Exception:
                continue
        # fallback unified
        for css in list_for_each_entry(
            "struct cgroup_subsys_state",
            cgroup.subsys.address_of_(),
            "cgroup_node",
        ):
            return f"{_to_str(cgroup_path(css.cgroup)).strip()}\n"
    except Exception:
        pass
    return "/\n"


def _fmt_unlimited(v: int) -> str:
    # rlim_t in kernel uses RLIM_INFINITY; represent as "unlimited"
    return "unlimited" if v < 0 or v >= (1 << 63) else str(v)


def generate_proc_pid_limits(prog, task) -> str:
    out: List[str] = []
    out.append(f"{'Limit':<25} {'Soft Limit':<20} {'Hard Limit':<20} {'Units':<15}\n")
    try:
        signal = task.signal
        rlim = signal.rlim
    except Exception:
        return "".join(out)

    for i, (name, unit) in enumerate(RLIMIT_NAMES):
        try:
            soft = int(rlim[i].rlim_cur.value_())
            hard = int(rlim[i].rlim_max.value_())
        except Exception:
            continue
        out.append(
            f"{name:<25} {_fmt_unlimited(soft):<20} {_fmt_unlimited(hard):<20} {unit:<15}\n"
        )
    return "".join(out)


def generate_proc_pid_mountinfo(prog, task) -> str:
    # This is complicated; keep best-effort minimal lines similar to inspiration
    out: List[str] = []
    try:
        from drgn.helpers.linux.list import list_for_each_entry
        from drgn.helpers.linux.fs import d_path

        nsproxy = task.nsproxy.read_()
        mnt_ns = nsproxy.mnt_ns
        for mount in list_for_each_entry(
            "struct mount", mnt_ns.mounts.address_of_(), "mnt_list"
        ):
            try:
                root_path = _to_str(d_path(mount.mnt_root.address_of_())).strip()
            except Exception:
                root_path = "/"
            try:
                mount_path = _to_str(d_path(mount.mnt_mountpoint.address_of_())).strip()
            except Exception:
                mount_path = "/"
            try:
                fstype = _to_str(mount.mnt.mnt_sb.s_type.name.string_()).strip()
            except Exception:
                fstype = "unknown"
            try:
                mnt_id = int(mount.mnt_id.value_())
            except Exception:
                mnt_id = 0
            try:
                parent_id = int(mount.mnt_parent.mnt_id.value_())
            except Exception:
                parent_id = 0
            out.append(f"{mnt_id} {parent_id} {mnt_id}:{mnt_id} {mount_path} {root_path} {fstype}\n")
    except Exception:
        pass
    return "".join(out)


def generate_proc_pid_oom_adj(prog, task) -> str:
    # Deprecated in modern kernels; keep 0 like inspiration
    return "0\n"


def generate_proc_pid_oom_score_adj(prog, task) -> str:
    try:
        return f"{int(task.signal.oom_score_adj.value_())}\n"
    except Exception:
        return "0\n"


def generate_proc_pid_oom_score(prog, task) -> str:
    # Real OOM scoring is complex; keep stable best-effort mapping like inspiration
    try:
        adj = int(task.signal.oom_score_adj.value_())
        return f"{adj * 1000}\n"
    except Exception:
        return "0\n"


def generate_proc_pid_stack(prog, task) -> str:
    trace = None
    try:
        trace = prog.stack_trace(task)
    except Exception:
        trace = "Stack trace not available\n"
    return trace


@emits_many("proc/{pid}/status", enumerator="enumerate_pids")
def emit_proc_pid_status(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return "# vmcore-report: stub (task not found)\n"
    return generate_proc_pid_status(prog, task)


@emits_many("proc/{pid}/cgroup", enumerator="enumerate_pids")
def emit_proc_pid_cgroup(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return "0::/\n"
    return generate_proc_pid_cgroup(prog, task)


@emits_many("proc/{pid}/cpuset", enumerator="enumerate_pids")
def emit_proc_pid_cpuset(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return "/\n"
    return generate_proc_pid_cpuset(prog, task)


@emits_many("proc/{pid}/limits", enumerator="enumerate_pids")
def emit_proc_pid_limits(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return f"{'Limit':<25} {'Soft Limit':<20} {'Hard Limit':<20} {'Units':<15}\n"
    return generate_proc_pid_limits(prog, task)


@emits_many("proc/{pid}/mountinfo", enumerator="enumerate_pids")
def emit_proc_pid_mountinfo(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return ""
    return generate_proc_pid_mountinfo(prog, task)


@emits_many("proc/{pid}/oom_adj", enumerator="enumerate_pids")
def emit_proc_pid_oom_adj(prog, pid: int) -> str:
    # Deprecated; always 0
    return generate_proc_pid_oom_adj(prog, None)


@emits_many("proc/{pid}/oom_score", enumerator="enumerate_pids")
def emit_proc_pid_oom_score(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return "0\n"
    return generate_proc_pid_oom_score(prog, task)


@emits_many("proc/{pid}/oom_score_adj", enumerator="enumerate_pids")
def emit_proc_pid_oom_score_adj(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return "0\n"
    return generate_proc_pid_oom_score_adj(prog, task)


@emits_many("proc/{pid}/stack", enumerator="enumerate_pids")
def emit_proc_pid_stack(prog, pid: int) -> str:
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, int(pid))
    if not task:
        return "Stack trace not available\n"
    return generate_proc_pid_stack(prog, task)
