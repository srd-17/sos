# This file is part of the sos project: https://github.com/sosreport/sos
#
# Drgn-based plugin to generate scheduler debug output analogous to
# /sys/kernel/debug/sched/debug from a vmcore.
#
# Output path in archive:
#   sys/kernel/debug/sched/debug
#
# Notes:
# - This adapts the provided drgn script into a sos vmcore-report plugin.
# - All "print(...)" statements are converted to buffer appends and the final
#   output is written to the path above.
# - Optional verbose annotations can be enabled by setting the environment
#   variable SOS_VMCORE_SCHED_DEBUG=1.

import os
import json

from sos.vmcore_report.plugin import DrgnPluginBase


class SchedDebugPlugin(DrgnPluginBase):
    plugin_name = "sched_debug"
    description = "Scheduler debug dump similar to /sys/kernel/debug/sched/debug"

    def collect(self, prog):
        # Controlled via env var to avoid introducing plugin option plumbing
        DEBUG = os.environ.get("SOS_VMCORE_SCHED_DEBUG", "0") in ("1", "true", "on", "yes")

        lines = []

        def add(s=""):
            # Accept bytes or str
            if isinstance(s, bytes):
                s = s.decode("utf-8", "ignore")
            if not s.endswith("\n"):
                s += "\n"
            lines.append(s)

        # Ported helper functions from the provided script
        def expl(t):
            return f"  // {t}" if DEBUG else ""

        def ns_to_ms(ns):
            """Convert nanoseconds to milliseconds"""
            try:
                return float(ns) / 1_000_000.0
            except Exception:
                return 0.0

        # Imports local to function to keep module import cheap if not used
        try:
            from drgn import cast
            from drgn.helpers.linux.cpumask import for_each_online_cpu
            from drgn.helpers.linux.percpu import per_cpu_ptr
            from drgn.helpers.linux.list import list_for_each_entry
            from drgn.helpers.linux.cgroup import cgroup_path
            from drgn.helpers.linux.sched import task_state_to_char
            from drgn.helpers.linux.pid import for_each_task
        except Exception as e:
            add(f"[ERROR] Unable to import drgn helpers: {e}")
            self.archive.add_string("".join(lines), "sys/kernel/debug/sched/debug", mode="w")
            return

        def print_rt_rq(rq, cpu):
            add(f"\nrt_rq[{cpu}]:")
            try:
                add(f"  .rt_nr_running                 : {rq.rt.rt_nr_running.value_()}{expl('Number of runnable RT tasks')}")
            except Exception as e:
                add(f"  [WARN] Unable to read rt fields: {e}")

        def print_dl_rq(rq, cpu):
            add(f"\ndl_rq[{cpu}]:")
            try:
                add(f"  .dl_nr_running                 : {rq.dl.dl_nr_running.value_()}{expl('Number of runnable DL tasks')}")
            except Exception as e:
                add(f"  [WARN] Unable to read dl_nr_running: {e}")
            try:
                add(f"  .dl_bw->bw                     : {rq.dl.max_bw.value_()}{expl('Maximum allowed DL bandwidth')}")
            except Exception as e:
                add(f"  [WARN] Unable to read max_bw: {e}")
            try:
                add(f"  .dl_bw->total_bw               : {rq.dl.this_bw.value_() + rq.dl.running_bw.value_()}{expl('Current reserved DL bandwidth')}")
            except Exception as e:
                add(f"  [WARN] Unable to read total_bw: {e}")

        def print_cpu_summary(rq, cpu):
            add(f"\ncpu#{cpu}")
            try:
                add(f"  .nr_running                    : {rq.nr_running.value_()} {expl('number of runnable tasks on this CPU')}")
            except Exception:
                pass
            try:
                add(f"  .nr_switches                   : {rq.nr_switches.value_()} {expl('total number of context switches')}")
            except Exception:
                pass
            try:
                add(f"  .nr_uninterruptible            : {rq.nr_uninterruptible.value_()} {expl('tasks in D-state (uninterruptible sleep)')}")
            except Exception:
                pass
            try:
                add(f"  .next_balance                  : {ns_to_ms(rq.next_balance.value_()):.6f}{expl('when this CPU will next perform load balancing')}")
            except Exception:
                pass
            try:
                add(f"  .curr->pid                     : {rq.curr.pid.value_()}{expl('currently running PID')}")
            except Exception:
                pass
            try:
                add(f"  .clock                         : {ns_to_ms(rq.clock.value_()):.6f}{expl('CPU local sched_clock()')}")
            except Exception:
                pass
            try:
                add(f"  .clock_task                    : {ns_to_ms(rq.clock_task.value_()):.6f}{expl('per-task scheduler clock (advances only while task runs)')}")
            except Exception:
                pass
            try:
                add(f"  .avg_idle                      : {rq.avg_idle.value_()}{expl('average idle duration before picking next task')}")
            except Exception:
                pass
            try:
                add(f"  .max_idle_balance_cost         : {rq.max_idle_balance_cost.value_()}{expl('max time allowed for load balancing')}")
            except Exception:
                pass

        def print_cfs_rq(cfs_rq, cpu, path="/"):
            add(f"\ncfs_rq[{cpu}]:{path}")

            def line(name, val):
                add(f"  .{name:30}: {val}")

            try:
                line("nr_running",        f"{cfs_rq.nr_running.value_()} {expl('tasks runnable in this CFS group')}")
            except Exception:
                pass
            try:
                line("h_nr_runnable",     f"{cfs_rq.h_nr_runnable.value_()} {expl('hierarchical runnable tasks including children groups')}")
            except Exception:
                pass
            try:
                line("h_nr_queued",       f"{cfs_rq.h_nr_queued.value_()} {expl('queued entities in this CFS group')}")
            except Exception:
                pass
            try:
                line("h_nr_delayed",      f"{cfs_rq.h_nr_delayed.value_()} {expl('tasks waiting due to throttling/delay')}")
            except Exception:
                pass
            try:
                line("idle_nr_running",   f"{cfs_rq.idle_nr_running.value_()} {expl('idle tasks in this group')}")
            except Exception:
                pass
            try:
                line("idle_h_nr_running", f"{cfs_rq.idle_h_nr_running.value_()} {expl('hierarchical idle tasks')}")
            except Exception:
                pass

            try:
                line("load",              f"{cfs_rq.load.weight.value_()} {expl('weight based on nice value')}")
            except Exception:
                pass

            try:
                avg = cfs_rq.avg
                line("load_avg",          f"{avg.load_avg.value_()} {expl('time decayed load average (1024 scaled)')}")
                line("runnable_avg",      f"{avg.runnable_avg.value_()} {expl('runnable time average')}")
                line("util_avg",          f"{avg.util_avg.value_()} {expl('actual CPU utilization')}")
                line("util_est",          f"{avg.util_est.value_()} {expl('predicted future CPU utilization')}")
            except Exception:
                pass

            try:
                rem = cfs_rq.removed
                line("removed.load_avg",      f"{rem.load_avg.value_()} {expl('load removed due to task exit/migration')}")
                line("removed.util_avg",      f"{rem.util_avg.value_()} {expl('usage removed after exit/migration')}")
                line("removed.runnable_avg",  f"{rem.runnable_avg.value_()} {expl('runnable removed due to exit/migration')}")
            except Exception:
                pass

            try:
                line("tg_load_avg_contrib",   f"{cfs_rq.tg_load_avg_contrib.value_()} {expl('cgroup contribution to parent load')}")
            except Exception:
                pass
            try:
                line("tg_load_avg",           f"{cfs_rq.last_update_tg_load_avg.value_()} {expl('total task group load including children')}")
            except Exception:
                pass

            try:
                line("throttled",             f"{cfs_rq.throttled.value_()} {expl('1 if cgroup is throttled (CPU quota hit)')}")
            except Exception:
                pass
            try:
                line("throttle_count",        f"{cfs_rq.throttle_count.value_()} {expl('number of throttling events')}")
            except Exception:
                pass

            try:
                se = cfs_rq.curr
                if se:
                    line("se->exec_start",       f"{ns_to_ms(se.exec_start.value_()):.6f} {expl('last time sched_entity started running')}")
                    line("se->vruntime",         f"{ns_to_ms(se.vruntime.value_()):.6f} {expl('vruntime = weighted CPU time consumed')}")
                    line("se->sum_exec_runtime", f"{ns_to_ms(se.sum_exec_runtime.value_()):.6f} {expl('total execution time of this entity')}")
                    line("se->load.weight",      f"{se.load.weight.value_()} {expl('load weight (based on nice)')}")
                    line("se->avg.load_avg",     f"{se.avg.load_avg.value_()} {expl('PELT load average')}")
                    line("se->avg.util_avg",     f"{se.avg.util_avg.value_()} {expl('PELT CPU utilization')}")
                    line("se->avg.runnable_avg", f"{se.avg.runnable_avg.value_()} {expl('PELT runnable avg')}")
            except Exception:
                pass

        def walk_cgroup_cfs_rqs(rq, cpu):
            try:
                print_cfs_rq(rq.cfs, cpu, "/")
            except Exception as e:
                add(f"[WARN] Unable to dump root cfs_rq for CPU {cpu}: {e}")

            try:
                tg_list_head = prog["task_groups"].address_of_()
            except Exception as e:
                add(f"[WARN] Unable to access task_groups list: {e}")
                return

            try:
                for tg in list_for_each_entry("struct task_group", tg_list_head, "list"):
                    try:
                        if not tg.cfs_rq:
                            continue
                        cfs_rq = tg.cfs_rq[cpu]
                        cgrp = tg.css.cgroup
                        try:
                            path = cgroup_path(cgrp).decode()
                        except Exception:
                            path = "<unknown>"
                        print_cfs_rq(cfs_rq, cpu, path)
                    except Exception as e:
                        add(f"[ERROR] TG {hex(int(tg))}: {e}")
            except Exception as e:
                add(f"[WARN] Failed iterating task groups: {e}")

        def print_runqueue_info(rq, cpu):
            walk_cgroup_cfs_rqs(rq, cpu)
            print_rt_rq(rq, cpu)
            print_dl_rq(rq, cpu)

        def print_task_table_header():
            add("\nrunnable tasks:")
            add(" S  task             PID                 vruntime           eligible           deadline                    slice        sum-exec      switches  prio     wait-time     sum-sleep     sum-block  node  group-id  group-path")
            add("-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------")
            if DEBUG:
                add("// vruntime = weighted runtime")
                add("// eligible = whether task can run now")
                add("// deadline = vruntime + slice")
                add("// slice = ideal fair share")
                add("// sum-exec = total execution time")
                add("// wait-time = time spent waiting on CPU")

        def get_task_cgroup_path(task):
            try:
                cgrp = task.cgroups.dfl_cgrp
                return cgroup_path(cgrp).decode()
            except Exception:
                return "/"

        def print_task_info(task):
            try:
                state = task_state_to_char(task)
            except Exception:
                state = "?"
            try:
                comm = task.comm.string_().decode(errors="ignore")[:16].ljust(16)
            except Exception:
                comm = "<unknown>        "
            try:
                pid = task.pid.value_()
            except Exception:
                pid = -1

            try:
                se = task.se
                vruntime = ns_to_ms(se.vruntime.value_())
                eligible = "E" if int(task.se.on_rq) >= 0 else "-"
                sum_exec = ns_to_ms(se.sum_exec_runtime.value_())
                _slice = ns_to_ms(se.slice.value_())
                deadline = vruntime + _slice
            except Exception:
                vruntime = 0.0
                eligible = "-"
                sum_exec = 0.0
                _slice = 0.0
                deadline = 0.0

            try:
                wait_time = ns_to_ms(task.stats.wait_sum.value_())
            except Exception:
                wait_time = 0.0
            try:
                sum_sleep = ns_to_ms(task.stats.sum_sleep_runtime.value_())
            except Exception:
                sum_sleep = 0.0
            try:
                sum_block = ns_to_ms(task.stats.sum_block_runtime.value_())
            except Exception:
                sum_block = 0.0

            try:
                prio = task.prio.value_()
            except Exception:
                prio = 0

            try:
                nvcsw = task.nvcsw.value_()
            except Exception:
                nvcsw = 0
            try:
                nivcsw = task.nivcsw.value_()
            except Exception:
                nivcsw = 0
            switches = nvcsw + nivcsw

            cgroup = get_task_cgroup_path(task)

            add(f" {state} {comm:<16}{pid:>6} {vruntime:30.6f}   {eligible:<1}   {deadline:30.6f}       {_slice:>8.6f}   {sum_exec:>12.6f} {switches:>8}   {prio:>3}   {wait_time:>10.6f}   {sum_sleep:>10.6f}   {sum_block:>10.6f}   {0:>2}    {0:>3}    {cgroup}")

        def print_tasks_for_cpu(cpu):
            print_task_table_header()
            try:
                for task in for_each_task(prog):
                    try:
                        if task.on_cpu.value_() == cpu:
                            print_task_info(task)
                    except Exception:
                        # task may be partially corrupted in vmcore; continue
                        continue
            except Exception as e:
                add(f"[WARN] Failed iterating tasks for CPU {cpu}: {e}")

        # Main body
        try:
            # CPU iteration with robust fallbacks (online -> possible -> nr_cpu_ids -> 0)
            def iter_cpus():
                # Preferred: online CPUs
                try:
                    for c in for_each_online_cpu(prog):
                        yield int(c)
                    return
                except Exception:
                    pass
                # Fallback: present CPUs
                try:
                    from drgn.helpers.linux.cpumask import for_each_present_cpu
                    for c in for_each_present_cpu(prog):
                        yield int(c)
                    return
                except Exception:
                    pass
                # Fallback: possible CPUs
                try:
                    from drgn.helpers.linux.cpumask import for_each_possible_cpu
                    for c in for_each_possible_cpu(prog):
                        yield int(c)
                    return
                except Exception:
                    pass
                # Fallback: nr_cpu_ids
                try:
                    n = int(prog['nr_cpu_ids'].value_())
                    for c in range(n):
                        yield c
                    return
                except Exception:
                    pass
                # Last resort
                yield 0

            # Resolve per-cpu runqueue symbol across kernels
            def get_rq_for_cpu(cpu):
                last_err = None
                for sym in ('runqueues', 'rq'):
                    try:
                        return per_cpu_ptr(prog[sym].address_of_(), cpu)
                    except Exception as e:
                        last_err = e
                        continue
                add(f"[ERROR] Unable to locate per-cpu runqueue for CPU {cpu}: {last_err}")
                return None

            for cpu in iter_cpus():
                rq = get_rq_for_cpu(cpu)
                if rq is None:
                    continue
                # Optional CPU summary
                try:
                    print_cpu_summary(rq, cpu)
                except Exception:
                    pass
                # Detailed runqueue info and runnable tasks on this CPU
                print_runqueue_info(rq, cpu)
                print_tasks_for_cpu(cpu)
        except Exception as e:
            add(f"[ERROR] Top-level scheduler dump failed: {e}")

        # Write to requested path inside the archive
        self.archive.add_string("".join(lines), "sys/kernel/debug/sched/debug", mode="w")
        # Also leave a small index under the plugin dir for discoverability
        summary = {"output": "sys/kernel/debug/sched/debug", "debug_annotations": bool(DEBUG)}
        self.write_text("index.json", json.dumps(summary, indent=2))
