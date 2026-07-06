---
source: "Redis Documentation: Persistence"
url: https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/
service: "redis"
section: persistence
license: "Redis Source Available License v2 / BSD-3-Clause"
retrieved: "2026-07-06"
---

# Redis Persistence Configuration

Redis supports two primary persistence mechanisms -- RDB snapshots and Append-Only File (AOF) -- each with distinct trade-offs. A hybrid approach combining both is recommended for production systems that require durability.

## RDB Snapshots

RDB persistence produces point-in-time snapshots of the dataset at configured intervals. The `save` directive defines the trigger conditions using the format `save <seconds> <changes>`. Multiple conditions can be specified, and a snapshot is taken when any one condition is met:

```
save 3600 1      # snapshot after 3600 seconds if at least 1 key changed
save 300 100     # snapshot after 300 seconds if at least 100 keys changed
save 60 10000    # snapshot after 60 seconds if at least 10000 keys changed
```

**Advantages:** RDB files are compact single-file representations of the dataset, making them ideal for backups and disaster recovery. Restoring from RDB is significantly faster than replaying an AOF log, especially for large datasets. The fork-based snapshotting process has minimal impact on the parent process serving requests.

**Disadvantages:** Data written between snapshots is lost if Redis terminates unexpectedly. The data loss window depends on how frequently snapshots are triggered. For workloads that cannot tolerate any data loss, RDB alone is insufficient.

## Append-Only File (AOF)

AOF persistence logs every write operation received by the server, providing a more durable record. Enable it with `appendonly yes`. The `appendfsync` directive controls how frequently the AOF buffer is flushed to disk:

- **always** -- Fsync after every write command. Maximum durability but highest latency impact. Suitable for workloads where no data loss is acceptable.
- **everysec** -- Fsync once per second (the default and recommended setting). Balances durability with performance; at most one second of data may be lost.
- **no** -- Lets the operating system decide when to flush. Fastest but least durable; data loss depends on OS flush intervals.

### AOF Rewrite

Over time the AOF file grows as operations accumulate. Redis periodically rewrites the AOF to produce a minimal version containing only the commands needed to reconstruct the current dataset. Two directives control automatic rewrites:

- `auto-aof-rewrite-percentage 100` -- Triggers a rewrite when the AOF file has grown by this percentage since the last rewrite.
- `auto-aof-rewrite-min-size 64mb` -- The minimum AOF size before automatic rewrites are considered.

### Avoiding Latency Spikes During Rewrite

Setting `no-appendfsync-on-rewrite yes` prevents Redis from performing fsync on the main AOF file while a background rewrite or RDB save is in progress. During these operations, the disk is already under heavy I/O load, and additional fsync calls can cause noticeable latency spikes. Enabling this option trades a small window of reduced durability for smoother latency behavior during background operations.

## Hybrid Persistence (Recommended)

Using both RDB and AOF together provides the strongest durability guarantee. The AOF captures all writes for minimal data loss, while the RDB snapshot serves as a compact backup for faster recovery. When both are enabled and an AOF file exists at startup, Redis prioritises loading the AOF since it is typically more complete. This combination is the recommended configuration for production deployments where both durability and efficient recovery are important.

To disable persistence entirely (for pure caching), remove all `save` directives and set `appendonly no`.
