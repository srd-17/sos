# `vmcore-report` progress tracker (coverage vs `sos report`)

This page tracks progress for reconstructing a sosreport-like archive from a
**vmcore** using `sos vmcore-report`.

The goal is not byte-for-byte parity with `sos report`, but a **useful subset**
of artifacts that can be **reconstructed from kernel memory**.

This tracker is organized by the top-level archive namespaces that sosreport
generates:

- `proc/` — virtual `/proc` text files reconstructed from kernel state
- `sys/` — selected `/sys`-like or kernel-internal state summaries
- `sos_commands/` — “command-like” summaries produced from kernel state
  (these are not real command outputs)

---

## Status legend

- ✅ Implemented in vmcore-report
- 🟡 Partial / best-effort
- ❌ Not yet implemented
- 🚫 Not feasible from vmcore (likely needs live filesystem/userspace)

---

## `proc/` coverage

| Path | Status | Notes |
|---|---:|---|
| `proc/cmdline` | ✅ | via `saved_command_line` |
| `proc/cpuinfo` | ✅ | already present in repo |
| `proc/meminfo` | ✅ | full field set (memavailable, hugetlb, directmap, etc.) |
| `proc/modules` | ✅ | already present in repo |
| `proc/mounts` | 🟡 | reconstructs src/target/fstype; options best-effort; dump/pass=0 |
| `proc/interrupts` | 🟡 | per-IRQ per-CPU counts best-effort; type/label best-effort |
| `proc/softirqs` | 🟡 | uses per-cpu kernel_stat softirq counters when available |
| `proc/buddyinfo` | ✅ | per-zone free_area order counts |
| `proc/slabinfo` | 🟡 | header + per-cache counts; several columns are best-effort |
| `proc/<pid>/status` | ✅ | based on pid enumeration + task state reconstruction |
| `proc/vmstat` | ❌ | candidate from meminfo/mm stats arrays |
| `proc/zoneinfo` | ❌ | candidate via zones iteration + watermark/managed pages |
| `proc/locks` | ❌ | candidate via locking subsystem; kernel-version dependent |

---

## `sys/` coverage

| Path | Status | Notes |
|---|---:|---|
| `sys/kernel/sched_debug` | 🟡 | exists; can be expanded further |
| `sys/devices/system/cpu/*` | ❌ | most require sysfs reconstruction; may be partially feasible |
| `sys/fs/cgroup/*` | ❌ | memcg/cgroup internals are possible but complex |

---

## `sos_commands/` coverage

| Path group | Status | Notes |
|---|---:|---|
| `sos_commands/kernel/*` | 🟡 | framework exists; several candidates in drgn-tools (block/scsi/nvme/workqueue) |
| `sos_commands/hardware/*` | ❌ | mostly requires live userspace + hw tools |
| `sos_commands/networking/*` | ❌ | some may be feasible from kernel netns (crash_net.py) |

---

## Recommended next ports from drgn-tools

These drgn-tools modules contain logic that can be adapted into emitters:

### proc-like targets
- `drgn_tools/meminfo.py` → already ported (proc/meminfo)
- `drgn_tools/buddyinfo.py` → already ported (proc/buddyinfo)
- `drgn_tools/slabinfo.py` → already ported (proc/slabinfo)
- `drgn_tools/mounts.py` → already ported (proc/mounts)
- `drgn_tools/cmdline.py` → already ported (proc/cmdline)
- `drgn_tools/numastat.py` → could become `proc/numastat`
- `drgn_tools/sys.py` → could emit a small `sys/` summary (sysinfo-like)

### sos_commands-like targets
- `drgn_tools/block.py` → inflight I/O + block devices summary
- `drgn_tools/nvme.py` → NVMe controller/namespace/queue summaries
- `drgn_tools/scsi.py` → SCSI host/device/command summaries
- `drgn_tools/workqueue.py` → workqueue summaries
- `drgn_tools/lsmod.py` / `module.py` → richer module metadata

---

## How to use this tracker

1) Pick a missing artifact that is likely reconstructable from vmcore.
2) Find equivalent logic in:
   - existing vmcore-report emitters, or
   - `drgn.helpers.linux.*`, or
   - drgn-tools (`/home/opc/drgn-tools/drgn_tools`)
3) Implement a new emitter under:
   - `sos/vmcore_report/emitters/proc/`
   - `sos/vmcore_report/emitters/sys/`
   - `sos/vmcore_report/emitters/commands/`
4) Update this tracker table with ✅/🟡 and notes.

See also:
- `docs/vmcore-report-contributing-emitters.md`
- `docs/vmcore-report-execution-flow.md`
