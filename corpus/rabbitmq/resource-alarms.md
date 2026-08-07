---
source: "RabbitMQ Documentation: Memory Alarms and Disk Alarms"
url: https://www.rabbitmq.com/docs/memory
service: "rabbitmq"
section: memory
license: "Apache-2.0 (rabbitmq-website)"
retrieved: "2026-08-07"
---

# RabbitMQ Resource Alarms

RabbitMQ protects itself with *resource alarms*. When a node runs short of memory or disk, it blocks the connections that are publishing, while continuing to serve consumers. Producers are throttled until consumers drain the backlog and the alarm clears. This is a deliberate design choice: the broker degrades into back-pressure rather than being killed by the kernel or corrupting its message store.

## Memory: vm_memory_high_watermark

`vm_memory_high_watermark` is the threshold at which the memory alarm fires. It can be given two ways:

- **Relative** — `vm_memory_high_watermark.relative = 0.4` means 40% of available RAM. This is the recommended form because it survives the node being moved to a different host, and it is the form the default (0.6) uses.
- **Absolute** — `vm_memory_high_watermark.absolute = 2GB` pins a fixed byte figure, which is appropriate when the node shares a host with other memory-hungry processes.

The default of 0.6 is deliberately conservative. RabbitMQ's own accounting cannot see memory held by the Erlang runtime's allocators or by the operating system's page cache, so the headroom between the watermark and 100% absorbs that error. A watermark of 1.0 disables the protection: the alarm can never fire before the kernel's OOM killer reaches the node first.

In a container the "available RAM" figure is read from the cgroup limit, so `deploy.resources.limits.memory` in compose is what the relative watermark is a fraction of — not the host's total.

## Memory: paging

`vm_memory_high_watermark_paging_ratio` (default 0.5) sets when the broker begins paging messages out to disk, expressed as a fraction *of the watermark*, not of total memory. With a watermark of 0.4 and a ratio of 0.5, paging starts at 20% of RAM. Paging early smooths the transition into an alarm; paging late means the alarm arrives abruptly.

## Disk: disk_free_limit

`disk_free_limit.absolute` sets the free-space floor below which the disk alarm fires and publishing is blocked. The documentation recommends **at least as much free space as the node has RAM**, because a node under memory pressure may page its entire message backlog to disk. A value smaller than RAM can allow the disk to fill during paging, which risks corrupting the message store.

It accepts a byte figure with a unit suffix (`2GB`, `500MB`) or a relative form. Unlike the memory watermark, there is no safe way to disable it; a broker with no disk floor accepts publishes until the filesystem is full.

## Observing alarms

- `rabbitmq-diagnostics alarms` lists any alarm currently in effect.
- `rabbitmq-diagnostics memory_breakdown` attributes memory to queues, connections, binaries and the metadata store, which is how to tell a genuine backlog from a connection leak.
- `rabbitmq-diagnostics check_alarms` is a boolean form intended for health checks.

A node in alarm is *healthy but throttled*. Publishers see their connections blocked; a naive health check that only opens a connection will not notice.
