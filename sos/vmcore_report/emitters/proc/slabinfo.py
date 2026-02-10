# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/slabinfo reconstructed from vmcore via drgn.
#
# This is adapted from drgn-tools drgn_tools/slabinfo.py, but implemented
# without a runtime dependency on drgn-tools. We intentionally keep output
# aligned with /proc/slabinfo expectations (header + per-cache line).
#
# Note: /proc/slabinfo formatting differs from drgn-tools “table” output; we
# implement the kernel-style slabinfo output directly.

from __future__ import annotations

from typing import Iterator, List, Tuple

from drgn import FaultError, Object, cast
from drgn.helpers.linux.cpumask import for_each_present_cpu
from drgn.helpers.linux.nodemask import for_each_online_node
from drgn.helpers.linux.percpu import per_cpu_ptr
from drgn.helpers.linux.slab import _get_slab_cache_helper, for_each_slab_cache

from sos.vmcore_report.emitters.registry import emits


class FreelistError(Exception):
    cpu: int
    message: str


class FreelistFaultError(FreelistError):
    def __init__(self, cpu: int):
        self.cpu = cpu
        self.message = f"translation fault in freelist of cpu {cpu}"
        super().__init__(self.message)


class FreelistDuplicateError(FreelistError):
    ptr: int

    def __init__(self, cpu: int, ptr: int):
        self.cpu = cpu
        self.ptr = ptr
        self.message = f"duplicate freelist entry on cpu {cpu}: {ptr:016x}"
        super().__init__(self.message)


def _slab_type(prog):
    try:
        return prog.type("struct slab *")
    except Exception:
        return prog.type("struct page *")


def _has_struct_slab(prog) -> bool:
    if "has_struct_slab" not in prog.cache:
        try:
            prog.type("struct slab")
            prog.cache["has_struct_slab"] = True
        except Exception:
            prog.cache["has_struct_slab"] = False
    return bool(prog.cache["has_struct_slab"])


def _kmem_cache_pernode(cache: Object, nodeid: int) -> Tuple[int, int, int, int, int]:
    kmem_cache_node = cache.node[nodeid]
    prog = cache.prog_

    nr_slabs = int(kmem_cache_node.nr_slabs.counter.value_())
    nr_total_objs = int(kmem_cache_node.total_objects.counter.value_())
    nr_partial = int(kmem_cache_node.nr_partial.value_())

    slab_type = _slab_type(prog).type
    partial_slab_list = "slab_list" if slab_type.has_member("slab_list") else "lru"

    nr_free = 0
    node_total_use = 0
    from drgn.helpers.linux.list import list_for_each_entry  # type: ignore

    for page in list_for_each_entry(
        slab_type, kmem_cache_node.partial.address_of_(), partial_slab_list
    ):
        nrobj = int(page.objects.value_())
        nrinuse = int(page.inuse.value_())
        node_total_use += nrinuse
        nr_free += nrobj - nrinuse

    return nr_slabs, nr_total_objs, nr_partial, node_total_use, nr_free


def _kmem_cache_percpu(cache: Object) -> int:
    prog = cache.prog_
    use_slab = _has_struct_slab(prog)
    cpu_per_node = 0

    for cpuid in for_each_present_cpu(prog):
        per_cpu_slab = per_cpu_ptr(cache.cpu_slab, cpuid)
        cpu_slab_ptr = per_cpu_slab.slab if use_slab else per_cpu_slab.page
        if cpu_slab_ptr:
            cpu_per_node += 1

    return cpu_per_node


def _collect_node_info(cache: Object) -> Tuple[int, int, int, int, int]:
    nr_slabs = nr_total_objs = nr_free = nr_partial = node_total_use = 0
    prog = cache.prog_

    for node in for_each_online_node(prog):
        slabs, total_objs, partial, total_use, free = _kmem_cache_pernode(cache, int(node))
        nr_slabs += slabs
        nr_total_objs += total_objs
        nr_partial += partial
        node_total_use += total_use
        nr_free += free

    return nr_slabs, nr_total_objs, nr_partial, node_total_use, nr_free


def _slub_get_cpu_freelist_cnt(cpu_freelist: Object, slub_helper: Object, cpu: int) -> int:
    free = set()
    ptr = int(cpu_freelist.value_())
    freelist_offset = int(slub_helper._slab_cache.offset.value_())
    while ptr:
        if ptr in free:
            raise FreelistDuplicateError(cpu, ptr)
        free.add(ptr)
        ptr = int(slub_helper._freelist_dereference(ptr + freelist_offset))
    return len(free)


def _slub_per_cpu_partial_free(cpu_partial: Object) -> int:
    partial_free = 0
    type_ = _slab_type(cpu_partial.prog_)

    while cpu_partial:
        page = cast(type_, cpu_partial)
        partial_objects = int(page.objects.value_())
        partial_inuse = int(page.inuse.value_())
        partial_free += partial_objects - partial_inuse
        cpu_partial = page.next

    return partial_free


class _CpuSlubWrapper:
    def __init__(self, obj):
        self._obj = obj

    def __getattr__(self, key):
        if key == "cpu_slab":
            raise AttributeError("CpuSlubWrapper!")
        return getattr(self._obj, key)


def _kmem_cache_slub_info(cache: Object) -> Tuple[int, int, List[FreelistError]]:
    prog = cache.prog_
    use_slab = _has_struct_slab(prog)

    total_slabs = 0
    free_objects = 0

    slub_helper = _get_slab_cache_helper(_CpuSlubWrapper(cache))
    corrupt: List[FreelistError] = []

    for cpuid in for_each_present_cpu(prog):
        per_cpu_slab = per_cpu_ptr(cache.cpu_slab, cpuid)
        cpu_freelist = per_cpu_slab.freelist
        cpu_slab_ptr = per_cpu_slab.slab if use_slab else per_cpu_slab.page
        cpu_partial = per_cpu_slab.partial

        if not cpu_slab_ptr:
            continue

        page_inuse = int(cpu_slab_ptr.inuse.value_())
        objects = int(cpu_slab_ptr.objects.value_())
        if objects < 0:
            objects = 0
        free_objects += objects - page_inuse

        try:
            free_objects += _slub_get_cpu_freelist_cnt(cpu_freelist, slub_helper, int(cpuid))
        except FaultError:
            corrupt.append(FreelistFaultError(int(cpuid)))
        except FreelistDuplicateError as e:
            corrupt.append(e)

        free_objects += _slub_per_cpu_partial_free(cpu_partial)
        total_slabs += 1

    return total_slabs, free_objects, corrupt


def _iter_slab_caches(prog) -> Iterator[Object]:
    for cache in for_each_slab_cache(prog):
        yield cache


def _emit_slabinfo_line(prog, cache: Object) -> str:
    # We approximate /proc/slabinfo "slabinfo - version: 2.1" layout:
    # name <active_objs> <num_objs> <objsize> <objperslab> <pagesperslab> ...
    #
    # We have strong data for active/total/objsize; objperslab/pagesperslab are
    # kernel-dependent; derive best-effort from cache->oo on SLUB.
    name = cache.name.string_().decode("utf-8", errors="replace")
    objsize = int(cache.object_size)

    total_slabs, free_objects, _ = _kmem_cache_slub_info(cache)
    nr_slabs, nr_total_objs, nr_partial, _, nr_free = _collect_node_info(cache)
    cpu_per_node = _kmem_cache_percpu(cache)

    full_slabs = nr_slabs - cpu_per_node - nr_partial
    free_objects += nr_free
    total_slabs += nr_partial + full_slabs
    active = nr_total_objs - free_objects
    if active < 0:
        active = 0

    # Derive obj_per_slab/pages_per_slab from cache.oo where possible.
    obj_per_slab = 0
    pages_per_slab = 0
    try:
        # SLUB: cache.oo is (order << OO_SHIFT) | objs_per_slab
        # OO_SHIFT is typically 16.
        oo = int(cache.oo.x)
        obj_per_slab = oo & 0xFFFF
        order = (oo >> 16) & 0xFFFF
        pages_per_slab = 1 << order
    except Exception:
        obj_per_slab = 0
        pages_per_slab = 0

    # Additional columns: tunables, slabdata - fill with 0/best-effort
    # This keeps parsers happy and is stable across kernels.
    return (
        f"{name: <21} "
        f"{active: >7} {nr_total_objs: >7} {objsize: >6} "
        f"{obj_per_slab: >4} {pages_per_slab: >4} "
        f": tunables {0: >4} {0: >4} {0: >4} : slabdata "
        f"{total_slabs: >6} {nr_slabs: >6} {0: >6}\n"
    )


@emits("proc/slabinfo")
def emit_proc_slabinfo(prog) -> str:
    out: List[str] = []
    out.append("slabinfo - version: 2.1\n")
    out.append(
        "# name            <active_objs> <num_objs> <objsize> <objperslab> <pagesperslab>"
        " : tunables <limit> <batchcount> <sharedfactor> : slabdata <active_slabs> <num_slabs> <sharedavail>\n"
    )
    try:
        for cache in _iter_slab_caches(prog):
            try:
                out.append(_emit_slabinfo_line(prog, cache))
            except Exception:
                continue
    except Exception as e:
        return f"# vmcore-report: stub (/proc/slabinfo not reconstructable) - {e}\n"

    return "".join(out)
