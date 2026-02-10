# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emitter registry and discovery for vmcore-report (proc/, sys/, sos_commands/).
#
# Goals:
# - Decentralized: contributors add emitters in per-domain modules without editing a central map
# - Discoverable: registry imports all modules under emitters.{proc,sys,commands}
# - Stable API: simple decorators for fixed-path and templated-path emitters
# - Best-effort: enumerators handle kernel variation; emitters return text; writer handles archive IO

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple


# Decorators (public API for emitters)
def emits(path: str):
    """Annotate a function as a fixed-path emitter (produce(prog) -> str)."""
    def _wrap(fn: Callable):
        setattr(fn, "_emit_path", path)
        setattr(fn, "_emit_enumerator", None)
        return fn
    return _wrap


def emits_many(path_template: str, enumerator: str):
    """Annotate a function as a templated-path emitter (produce(prog, **keys) -> str).

    Example:
      @emits_many("proc/{pid}/status", enumerator="enumerate_pids")
      def produce_status(prog, pid: int) -> str: ...
    """
    def _wrap(fn: Callable):
        setattr(fn, "_emit_path", path_template)
        setattr(fn, "_emit_enumerator", enumerator)
        return fn
    return _wrap


@dataclass(frozen=True)
class Emitter:
    path: str                         # fixed path or template
    enumerator: Optional[str]         # None for fixed, else enumerator name
    func: Callable                    # produce(prog) or produce(prog, **keys)


# Discovery utilities
def _iter_submodules(pkg_name: str) -> Iterable[object]:
    """Yield imported submodules under the given package."""
    try:
        pkg = importlib.import_module(pkg_name)
    except Exception:
        return
    if not hasattr(pkg, "__path__"):
        return
    for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
        fq = f"{pkg_name}.{modname}"
        try:
            yield importlib.import_module(fq)
        except Exception:
            # best-effort: skip broken modules
            continue


def _collect_emitters_from_module(mod) -> List[Emitter]:
    emitters: List[Emitter] = []
    for obj in vars(mod).values():
        if callable(obj) and hasattr(obj, "_emit_path"):
            emitters.append(
                Emitter(
                    path=getattr(obj, "_emit_path"),
                    enumerator=getattr(obj, "_emit_enumerator"),
                    func=obj,
                )
            )
    return emitters


def list_emitters(scope: str) -> List[Emitter]:
    """List all discovered emitters for a scope: 'proc', 'sys', or 'commands'."""
    if scope not in ("proc", "sys", "commands"):
        raise ValueError(f"invalid scope: {scope}")
    base_pkg = f"sos.vmcore_report.emitters.{scope}"
    discovered: List[Emitter] = []
    for mod in _iter_submodules(base_pkg) or ():
        discovered.extend(_collect_emitters_from_module(mod))
    return discovered


# Built-in enumerators (names referenced by @emits_many)
def enumerate_pids(prog) -> Iterable[Dict[str, int]]:
    """Yield {'pid': pid} for each PID, best-effort.

    Enumeration strategy (deterministic):
    1) Prefer drgn.helpers.linux.pid.for_each_pid(prog) if available
    2) Fallback to drgn.helpers.linux.pid.for_each_task(prog) and collect task.pid
    3) Last resort: init_task traversal

    Notes:
    - We filter out pid <= 0.
    - We deduplicate and yield in sorted order to keep archive output stable.
    - This will include thread IDs (TIDs) when falling back to for_each_task(),
      which matches how /proc exposes per-thread directories.
    """
    # 1) Preferred: for_each_pid (canonical PID list)
    #
    # Note: some drgn versions yield internal kernel 'struct pid *' objects or
    # pointers instead of numeric PIDs, which can produce huge numbers when
    # coerced to int(). We validate candidates by requiring they are in a sane
    # range (pid_max) and > 0, else fall back to for_each_task().
    try:
        from drgn.helpers.linux.pid import for_each_pid  # type: ignore

        pid_max = 1_000_000
        try:
            pid_max = int(prog["pid_max"].value_())
        except Exception:
            pass

        pset = set()
        for p in for_each_pid(prog):
            cand = None
            try:
                if isinstance(p, tuple) and len(p) >= 1:
                    cand = int(p[0])
                else:
                    cand = int(p)
            except Exception:
                try:
                    cand = int(p.value_())
                except Exception:
                    cand = None

            if cand is None:
                continue
            if 0 < cand <= pid_max:
                pset.add(cand)

        # If the helper didn't yield sane numeric PIDs, fall back.
        if pset:
            for pid in sorted(pset):
                yield {"pid": pid}
            return
    except Exception:
        pass

    # 2) Fallback: for_each_task (collect task.pid)
    try:
        from drgn.helpers.linux.pid import for_each_task  # type: ignore

        pset = set()
        for task in for_each_task(prog):
            try:
                pid = int(task.pid.value_())
            except Exception:
                continue
            if pid > 0:
                pset.add(pid)

        for pid in sorted(pset):
            yield {"pid": pid}
        return
    except Exception:
        pass

    # 3) Last resort: try init_task traversal if present
    try:
        init_task = prog["init_task"]
        # next_task list traversal; keep it defensive
        seen = set()
        task = init_task
        for _ in range(1024 * 1024):  # hard cap to avoid infinite loops on corrupt lists
            try:
                pid = int(task.pid.value_())
                if pid not in seen and pid > 0:
                    seen.add(pid)
                # next = task.tasks.next (struct list_head) -> container_of
                next_tasks = task.tasks.next
                if next_tasks == init_task.tasks.address_of_():
                    break
                task = next_tasks.entry  # may raise on some kernels; best-effort
            except Exception:
                break
        for pid in sorted(seen):
            yield {"pid": pid}
        return
    except Exception:
        # No pids found
        return []


def _nr_cpu_ids(prog) -> Optional[int]:
    try:
        return int(prog["nr_cpu_ids"].value_())
    except Exception:
        return None


def enumerate_cpus(prog) -> Iterable[Dict[str, int]]:
    """Yield {'cpu': cpu_id} for each cpu, best-effort."""
    # Try cpu_online_mask if available to get accurate set
    try:
        from drgn.helpers.linux import cpumask_to_cpus  # type: ignore
        mask = prog["cpu_online_mask"]
        for cpu in cpumask_to_cpus(mask):
            yield {"cpu": int(cpu)}
        return
    except Exception:
        pass

    # Fallback to nr_cpu_ids or a single CPU
    count = _nr_cpu_ids(prog)
    if isinstance(count, int) and count > 0:
        for cpu in range(count):
            yield {"cpu": cpu}
    else:
        yield {"cpu": 0}


_ENUMS: Dict[str, Callable] = {
    "enumerate_pids": enumerate_pids,
    "enumerate_cpus": enumerate_cpus,
}


def _format_stub(reason: str) -> str:
    return f"# vmcore-report: stub (not reconstructable from vmcore) - {reason}\n"


def _safe_format_path(template: str, keys: Dict[str, object]) -> str:
    try:
        return template.format(**keys)
    except Exception:
        # If formatting fails, degrade to a safe path (avoid braces)
        return template.replace("{", "_").replace("}", "_")


def run_scope(scope: str, prog, logger=None) -> List[Tuple[str, str]]:
    """Resolve, produce, and return a list of (dest_path, text) for the given scope.

    - scope: 'proc', 'sys', or 'commands'
    - prog: drgn.Program
    - logger: optional logger for warnings/errors
    """
    outputs: List[Tuple[str, str]] = []
    for em in list_emitters(scope):
        try:
            if em.enumerator:
                enum_fn = _ENUMS.get(em.enumerator)
                if not enum_fn:
                    # Missing enumerator: create a stub to preserve presence
                    path = em.path.replace("{", "_").replace("}", "_")
                    outputs.append((path, _format_stub(f"enumerator '{em.enumerator}' not found")))
                    if logger:
                        logger.warning(f"Enumerator '{em.enumerator}' not found for {em.func.__name__}")
                    continue
                for keys in enum_fn(prog) or ():
                    path = _safe_format_path(em.path, keys)
                    try:
                        body = em.func(prog, **keys)
                    except Exception as e:
                        if logger:
                            logger.debug(f"Emitter {em.func.__name__} failed for {path}: {e}")
                        body = _format_stub(f"emitter error: {e}")
                    if not isinstance(body, str):
                        body = str(body)
                    outputs.append((path, body))
            else:
                body = em.func(prog)
                if not isinstance(body, str):
                    body = str(body)
                outputs.append((em.path, body))
        except Exception as e:
            # Ensure one file is still produced to preserve presence
            path = em.path if "{" not in em.path else em.path.replace("{", "_").replace("}", "_")
            outputs.append((path, _format_stub(f"emitter failure: {e}")))
            if logger:
                logger.debug(f"Emitter {em.func.__name__} failed: {e}")
    return outputs
