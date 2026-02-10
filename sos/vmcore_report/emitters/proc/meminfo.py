# This file is part of the sos project: https://github.com/sosreport/sos
#
# Emit /proc/meminfo (best-effort) from vmcore using drgn helpers.
#
# Goal: replicate (as closely as practical) the field set and formatting of the
# drgn-tools meminfo module without depending on drgn-tools.
#
# Output units:
# - /proc/meminfo prints kB
# - Most counters we read are "pages" and are converted using PAGE_SHIFT.

from __future__ import annotations

from typing import Dict, Iterable, Iterator

from sos.vmcore_report.emitters.registry import emits


def _page_shift(prog) -> int:
    try:
        return int(prog.constant("PAGE_SHIFT").value_())
    except Exception:
        try:
            return int(prog["PAGE_SHIFT"].value_())
        except Exception:
            return 12


def _pages_to_kb(pages: int, page_shift: int) -> int:
    if pages < 0:
        pages = 0
    return int(pages) << max(0, page_shift - 10)


def _read_vmstat_array(prog, arr_name: str, enum_type: str) -> Dict[str, int]:
    arr = prog[arr_name].read_()
    enum_obj = prog.type(enum_type)
    stats: Dict[str, int] = {}
    for name, value in enum_obj.enumerators[:-1]:
        try:
            stats[name] = max(0, int(arr[value].counter.value_()))
        except Exception:
            try:
                stats[name] = max(0, int(arr[value].value_()))
            except Exception:
                continue
    return stats


def _get_global_mm_stats(prog) -> Dict[str, int]:
    stats: Dict[str, int] = {}
    try:
        stats.update(_read_vmstat_array(prog, "vm_zone_stat", "enum zone_stat_item"))
    except Exception:
        try:
            stats.update(_read_vmstat_array(prog, "vm_stat", "enum zone_stat_item"))
        except Exception:
            pass
    try:
        stats.update(_read_vmstat_array(prog, "vm_node_stat", "enum node_stat_item"))
    except Exception:
        pass
    try:
        stats.update(_read_vmstat_array(prog, "vm_numa_stat", "enum numa_stat_item"))
    except Exception:
        pass
    return stats


def _totalram_pages(prog) -> int:
    try:
        from drgn.helpers.linux.mm import totalram_pages  # type: ignore

        return int(totalram_pages(prog))
    except Exception:
        pass
    try:
        return int(prog["totalram_pages"].value_())
    except Exception:
        return 0


def _percpu_counter_sum_best_effort(counter_obj) -> int:
    try:
        from drgn.helpers.linux.percpu import percpu_counter_sum  # type: ignore

        return int(percpu_counter_sum(counter_obj))
    except Exception:
        try:
            return int(counter_obj.counter.value_())
        except Exception:
            return 0


def _has_member(obj, name: str) -> bool:
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


def _iter_zones(prog) -> Iterator[object]:
    """Best-effort iteration of struct zone objects across nodes."""
    try:
        node_data = prog["node_data"]
        max_nodes = len(prog["preferred_node_policy"])
        for i in range(max_nodes):
            try:
                if node_data[i].value_() == 0:
                    continue
                node = node_data[i]
                try:
                    nr = int(node.nr_zones.value_())
                except Exception:
                    nr = int(node.nr_zones)
                for j in range(nr):
                    yield node.node_zones[j]
            except Exception:
                continue
    except Exception:
        return


def _get_total_available_pages(prog, s: Dict[str, int]) -> int:
    total_free_pages = int(s.get("NR_FREE_PAGES", 0))
    try:
        total_reserve_pages = int(prog["totalreserve_pages"].value_())
    except Exception:
        total_reserve_pages = 0

    available_pages = total_free_pages - total_reserve_pages

    # global low watermark
    low_wmark = 0
    try:
        wmark_low = int(prog.constant("WMARK_LOW"))
        for zone in _iter_zones(prog) or ():
            try:
                if _has_member(zone, "_watermark"):
                    low_wmark += int(zone._watermark[wmark_low].value_())
                else:
                    low_wmark += int(zone.watermark[wmark_low].value_())
            except Exception:
                continue
    except Exception:
        low_wmark = 0

    lru_active_file = int(s.get("NR_ACTIVE_FILE", 0))
    lru_inactive_file = int(s.get("NR_INACTIVE_FILE", 0))
    pagecache = lru_active_file + lru_inactive_file
    pagecache -= min(pagecache // 2, low_wmark)
    available_pages += pagecache

    # reclaimable slab + misc
    reclaimable_pages = int(
        s.get("NR_SLAB_RECLAIMABLE", s.get("NR_SLAB_RECLAIMABLE_B", 0))
    )
    reclaimable_pages += int(s.get("NR_KERNEL_MISC_RECLAIMABLE", 0))
    reclaimable_pages -= min(reclaimable_pages // 2, low_wmark)
    available_pages += reclaimable_pages

    # indirectly reclaimable bytes -> pages
    try:
        indr_bytes = int(s.get("NR_INDIRECTLY_RECLAIMABLE_BYTES", 0))
        if indr_bytes:
            available_pages += indr_bytes >> _page_shift(prog)
    except Exception:
        pass

    return max(0, int(available_pages))


def _get_block_dev_pages(prog) -> int:
    ret = 0
    try:
        from drgn.helpers.linux.list import list_for_each_entry  # type: ignore

        if "all_bdevs" in prog:
            for bdev in list_for_each_entry(
                "struct block_device", prog["all_bdevs"].address_of_(), "bd_list"
            ):
                try:
                    ret += int(bdev.bd_inode.i_mapping.nrpages.value_())
                except Exception:
                    continue
        elif "blockdev_superblock" in prog:
            for inode in list_for_each_entry(
                "struct inode",
                prog["blockdev_superblock"].s_inodes.address_of_(),
                "i_sb_list",
            ):
                try:
                    ret += int(inode.i_mapping.nrpages.value_())
                except Exception:
                    continue
    except Exception:
        pass
    return ret


def _get_total_swap_cache_pages(prog, s: Dict[str, int]) -> int:
    if "NR_SWAPCACHE" in s:
        return int(s.get("NR_SWAPCACHE", 0))
    # Fallback: sum swapper_spaces[*].nrpages best-effort
    ret = 0
    try:
        swapper_spaces = prog["swapper_spaces"]
        if "swap_cgroup_ctrl" in prog:
            max_types = len(prog["swap_cgroup_ctrl"])
        else:
            max_types = len(swapper_spaces)
        if "nr_swapper_spaces" in prog:
            nr_swapper_spaces = prog["nr_swapper_spaces"]
            for i in range(max_types):
                try:
                    nr = int(nr_swapper_spaces[i])
                    spaces = swapper_spaces[i]
                    if nr == 0 or spaces.value_() == 0:
                        continue
                    for j in range(nr):
                        ret += int(spaces[j].nrpages.value_())
                except Exception:
                    continue
        else:
            for i in range(max_types):
                try:
                    ret += int(swapper_spaces[i].nrpages.value_())
                except Exception:
                    continue
    except Exception:
        pass
    return ret


def _get_total_hugetlb_pages(prog) -> int:
    ret = 0
    try:
        if "hstates" not in prog or "hugetlb_max_hstate" not in prog:
            return 0
        hstates = prog["hstates"].read_()
        for i in range(int(prog["hugetlb_max_hstate"].value_())):
            try:
                h = hstates[i]
                pages_per = 1 << int(h.order.value_())
                ret += int(h.nr_huge_pages.value_()) * pages_per
            except Exception:
                continue
    except Exception:
        return 0
    return ret


def _get_vm_commit_limit(prog) -> int:
    allowed = 0
    try:
        total_pages = _totalram_pages(prog)
        total_swap_pages = int(prog["total_swap_pages"].value_())
        overcommit_kbytes = int(prog["sysctl_overcommit_kbytes"].value_())
        overcommit_ratio = int(prog["sysctl_overcommit_ratio"].value_())
        if overcommit_kbytes:
            allowed = overcommit_kbytes >> max(0, _page_shift(prog) - 10)
        else:
            hugetlb_pages = _get_total_hugetlb_pages(prog)
            allowed = (total_pages - hugetlb_pages) * overcommit_ratio // 100
        allowed += total_swap_pages
    except Exception:
        allowed = 0
    return int(allowed)


def _get_trans_hpage_unit(prog) -> int:
    # Try to match drgn-tools behavior via memory_stats ratio, else fallback to HPAGE_PMD_NR.
    try:
        page_shift = _page_shift(prog)
        page_size = 1 << page_shift
        hpage_pmd_nr = 1 << (21 - page_shift)  # PMD_SHIFT=21 on x86_64
        for item in prog["memory_stats"]:
            try:
                if item.name.string_().decode("utf-8") == "anon_thp":
                    if hasattr(item, "ratio") and int(item.ratio.value_()) != page_size:
                        return hpage_pmd_nr
                    return 1
            except Exception:
                continue
        return 1
    except Exception:
        try:
            return 1 << (21 - _page_shift(prog))
        except Exception:
            return 512


def _get_vmalloc_total_kb(prog) -> int:
    try:
        import drgn  # local import

        if prog.platform.arch != drgn.Architecture.X86_64:
            return -1
    except Exception:
        return -1

    vmalloc_start = 0xFFFFC90000000000
    vmalloc_end = 0xFFFFE8FFFFFFFFFF
    try:
        if "vmalloc_base" in prog:
            vmalloc_start = int(prog["vmalloc_base"].value_())
            try:
                from drgn.helpers.linux.boot import pgtable_l5_enabled  # type: ignore

                vmalloc_size_tb = 12800 if pgtable_l5_enabled(prog) else 32
                vmalloc_end = vmalloc_start + (vmalloc_size_tb << 40) - 1
            except Exception:
                vmalloc_end = vmalloc_start + (32 << 40) - 1
    except Exception:
        pass
    return int((vmalloc_end - vmalloc_start) >> 10)


def _format_kb_line(name: str, kb: int, width: int = 8) -> str:
    return f"{name + ':': <15} {str(int(kb)): >{width}} kB\n"


def _format_pages_line(prog, name: str, pages: int) -> str:
    ps = _page_shift(prog)
    return _format_kb_line(name, _pages_to_kb(int(pages), ps))


def _meminfo_kv(prog) -> Dict[str, int]:
    s = _get_global_mm_stats(prog)
    stats: Dict[str, int] = {}

    # basic
    stats["MemTotal"] = _totalram_pages(prog)
    stats["MemFree"] = int(s.get("NR_FREE_PAGES", 0))
    stats["MemAvailable"] = _get_total_available_pages(prog, s)

    file_pages = int(s.get("NR_FILE_PAGES", 0))
    swap_cache_pages = _get_total_swap_cache_pages(prog, s)
    buffer_pages = _get_block_dev_pages(prog)
    stats["Buffers"] = buffer_pages
    stats["Cached"] = max(0, file_pages - swap_cache_pages - buffer_pages)
    stats["SwapCached"] = swap_cache_pages

    # LRU
    lru_inactive_anon = int(s.get("NR_INACTIVE_ANON", 0))
    lru_active_anon = int(s.get("NR_ACTIVE_ANON", 0))
    lru_inactive_file = int(s.get("NR_INACTIVE_FILE", 0))
    lru_active_file = int(s.get("NR_ACTIVE_FILE", 0))
    lru_unevictable = int(s.get("NR_UNEVICTABLE", 0))

    stats["Active"] = lru_active_anon + lru_active_file
    stats["Inactive"] = lru_inactive_anon + lru_inactive_file
    stats["Active(anon)"] = lru_active_anon
    stats["Inactive(anon)"] = lru_inactive_anon
    stats["Active(file)"] = lru_active_file
    stats["Inactive(file)"] = lru_inactive_file
    stats["Unevictable"] = lru_unevictable
    stats["Mlocked"] = int(s.get("NR_MLOCK", 0))

    # swap + misc
    nr_to_be_unused = 0
    try:
        nr_swapfiles = int(prog["nr_swapfiles"].value_())
        for i in range(nr_swapfiles):
            si = prog["swap_info"][i]
            si_swp_used = int(si.flags.value_()) & int(prog["SWP_USED"].value_())
            si_swp_writeok = int(si.flags.value_()) & int(prog["SWP_WRITEOK"].value_())
            if si_swp_used and not si_swp_writeok:
                nr_to_be_unused += int(si.inuse_pages.value_())
    except Exception:
        nr_to_be_unused = 0

    try:
        stats["SwapTotal"] = int(prog["total_swap_pages"].value_()) + nr_to_be_unused
    except Exception:
        stats["SwapTotal"] = 0
    try:
        stats["SwapFree"] = int(prog["nr_swap_pages"].counter.value_()) + nr_to_be_unused
    except Exception:
        stats["SwapFree"] = 0

    stats["Dirty"] = int(s.get("NR_FILE_DIRTY", 0))
    stats["Writeback"] = int(s.get("NR_WRITEBACK", 0))
    stats["AnonPages"] = int(s.get("NR_ANON_MAPPED", s.get("NR_ANON_PAGES", 0)))
    stats["Mapped"] = int(s.get("NR_FILE_MAPPED", 0))
    stats["Shmem"] = int(s.get("NR_SHMEM", 0))

    # slab
    slab_reclaimable = int(
        s.get("NR_SLAB_RECLAIMABLE", s.get("NR_SLAB_RECLAIMABLE_B", 0))
    )
    slab_unreclaimable = int(
        s.get("NR_SLAB_UNRECLAIMABLE", s.get("NR_SLAB_UNRECLAIMABLE_B", 0))
    )
    kernel_misc = int(s.get("NR_KERNEL_MISC_RECLAIMABLE", 0))
    stats["KReclaimable"] = slab_reclaimable + kernel_misc
    stats["Slab"] = slab_reclaimable + slab_unreclaimable
    stats["SReclaimable"] = slab_reclaimable
    stats["SUnreclaim"] = slab_unreclaimable

    # kernel stack/pagetables (kernel stack is KB in vmstat on many kernels)
    stats["KernelStack_kB"] = int(s.get("NR_KERNEL_STACK_KB", s.get("NR_KERNEL_STACK", 0)))
    stats["PageTables"] = int(s.get("NR_PAGETABLE", 0))
    stats["NFS_Unstable"] = int(s.get("NR_UNSTABLE_NFS", 0))
    stats["Bounce"] = int(s.get("NR_BOUNCE", 0))
    stats["WritebackTmp"] = int(s.get("NR_WRITEBACK_TEMP", 0))

    # commit + vmalloc + percpu + hwcorrupted
    stats["CommitLimit"] = _get_vm_commit_limit(prog)
    try:
        committed_as = _percpu_counter_sum_best_effort(prog["vm_committed_as"])
        stats["Committed_AS"] = max(0, int(committed_as))
    except Exception:
        stats["Committed_AS"] = 0

    stats["VmallocTotal_kB"] = _get_vmalloc_total_kb(prog)
    try:
        if "nr_vmalloc_pages" in prog:
            stats["VmallocUsed"] = int(prog["nr_vmalloc_pages"].counter.value_())
        else:
            stats["VmallocUsed"] = 0
    except Exception:
        stats["VmallocUsed"] = 0
    stats["VmallocChunk"] = 0

    try:
        stats["Percpu"] = int(prog["pcpu_nr_populated"].value_()) * int(
            prog["pcpu_nr_units"].value_()
        )
    except Exception:
        stats["Percpu"] = -1

    try:
        stats["HardwareCorrupted_kB"] = int(prog["num_poisoned_pages"].counter.value_()) << (
            _page_shift(prog) - 10
        )
    except Exception:
        stats["HardwareCorrupted_kB"] = 0

    # THP
    unit = _get_trans_hpage_unit(prog)
    if "NR_ANON_THPS" in s:
        stats["AnonHugePages"] = int(s.get("NR_ANON_THPS", 0)) * unit
        stats["ShmemHugePages"] = int(s.get("NR_SHMEM_THPS", 0)) * unit
        stats["ShmemPmdMapped"] = int(s.get("NR_SHMEM_PMDMAPPED", 0)) * unit
        stats["FileHugePages"] = int(s.get("NR_FILE_THPS", -1)) * unit
        stats["FilePmdMapped"] = int(s.get("NR_FILE_PMDMAPPED", -1)) * unit
    else:
        stats["AnonHugePages"] = int(s.get("NR_ANON_TRANSPARENT_HUGEPAGES", 0)) * unit
        stats["ShmemHugePages"] = -1
        stats["ShmemPmdMapped"] = -1
        stats["FileHugePages"] = -1
        stats["FilePmdMapped"] = -1

    # CMA
    try:
        stats["CmaTotal"] = int(prog["totalcma_pages"].value_())
    except Exception:
        stats["CmaTotal"] = 0
    stats["CmaFree"] = int(s.get("NR_FREE_CMA_PAGES", 0))

    return stats


def _format_meminfo(prog, stats: Dict[str, int]) -> str:
    out = []

    basic_meminfo_items = [
        "MemTotal",
        "MemFree",
        "MemAvailable",
        "Buffers",
        "Cached",
        "SwapCached",
        "Active",
        "Inactive",
        "Active(anon)",
        "Inactive(anon)",
        "Active(file)",
        "Inactive(file)",
        "Unevictable",
        "Mlocked",
        "SwapTotal",
        "SwapFree",
        "Dirty",
        "Writeback",
        "AnonPages",
        "Mapped",
        "Shmem",
        "KReclaimable",
        "Slab",
        "SReclaimable",
        "SUnreclaim",
        "KernelStack",
        "PageTables",
        "NFS_Unstable",
        "Bounce",
        "WritebackTmp",
        "CommitLimit",
        "Committed_AS",
        "VmallocTotal",
        "VmallocUsed",
        "VmallocChunk",
        "Percpu",
        "HardwareCorrupted",
    ]

    for item in basic_meminfo_items:
        if item == "KernelStack":
            out.append(_format_kb_line("KernelStack", int(stats.get("KernelStack_kB", 0))))
            continue
        if item == "VmallocTotal":
            vt = int(stats.get("VmallocTotal_kB", -1))
            if vt != -1:
                out.append(_format_kb_line("VmallocTotal", vt))
            continue
        if item == "HardwareCorrupted":
            out.append(_format_kb_line("HardwareCorrupted", int(stats.get("HardwareCorrupted_kB", 0)), width=5))
            continue
        if item == "Percpu":
            if int(stats.get("Percpu", -1)) == -1:
                continue
        if item in ("FileHugePages", "FilePmdMapped") and int(stats.get(item, -1)) == -1:
            continue

        out.append(_format_pages_line(prog, item, int(stats.get(item, 0))))

    for item in ("AnonHugePages", "ShmemHugePages", "ShmemPmdMapped", "FileHugePages", "FilePmdMapped", "CmaTotal", "CmaFree"):
        if int(stats.get(item, -1)) == -1:
            continue
        out.append(_format_pages_line(prog, item, int(stats.get(item, 0))))

    # Hugetlb/DirectMap (best-effort)
    try:
        if "hstates" in prog and "default_hstate_idx" in prog:
            hstate = prog["hstates"][int(prog["default_hstate_idx"].value_())]
            out.append(f"HugePages_Total:   {int(hstate.nr_huge_pages.value_()):5d}\n")
            out.append(f"HugePages_Free:    {int(hstate.free_huge_pages.value_()):5d}\n")
            out.append(f"HugePages_Rsvd:    {int(hstate.resv_huge_pages.value_()):5d}\n")
            out.append(f"HugePages_Surp:    {int(hstate.surplus_huge_pages.value_()):5d}\n")
            try:
                hp_size_kb = 1 << (int(hstate.order.value_()) + _page_shift(prog) - 10)
                out.append(f"Hugepagesize:   {hp_size_kb:8d} kB\n")
            except Exception:
                pass

            total_hugetlb_bytes = 0
            try:
                hstates = prog["hstates"].read_()
                for i in range(int(prog["hugetlb_max_hstate"].value_())):
                    h = hstates[i]
                    total_hugetlb_bytes += int(h.nr_huge_pages.value_()) * (
                        (1 << int(h.order.value_())) * (1 << _page_shift(prog))
                    )
                out.append(f"Hugetlb:        {int(total_hugetlb_bytes / 1024):8d} kB\n")
            except Exception:
                pass
    except Exception:
        pass

    try:
        if "direct_pages_count" in prog:
            dpc = prog["direct_pages_count"].read_()
            direct_4k = int(dpc[int(prog.constant("PG_LEVEL_4K"))].value_())
            direct_2m = int(dpc[int(prog.constant("PG_LEVEL_2M"))].value_())
            out.append(f"DirectMap4k:    {direct_4k << 2:8d} kB\n")
            out.append(f"DirectMap2M:    {direct_2m << 11:8d} kB\n")
            try:
                if int(prog["direct_gbpages"].value_()) != 0:
                    direct_1g = int(dpc[int(prog.constant("PG_LEVEL_1G"))].value_())
                    out.append(f"DirectMap1G:    {direct_1g << 20:8d} kB\n")
            except Exception:
                pass
    except Exception:
        pass

    return "".join(out)


@emits("proc/meminfo")
def emit_proc_meminfo(prog) -> str:
    try:
        stats = _meminfo_kv(prog)
        return _format_meminfo(prog, stats)
    except Exception as e:
        return f"# vmcore-report: stub (/proc/meminfo not reconstructable) - {e}\n"
