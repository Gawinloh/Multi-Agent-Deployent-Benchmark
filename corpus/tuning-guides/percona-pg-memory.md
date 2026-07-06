---
source: "Percona PostgreSQL Memory Tuning Notes"
url: https://www.percona.com/blog/tuning-postgresql-memory-configuration-parameters/
service: "postgres"
section: memory-tuning
license: "CC BY 4.0"
retrieved: "2026-07-06"
---

# Percona PostgreSQL Memory Tuning Guidance

## Total Memory Budget Planning

The operating system requires approximately 1-2GB of RAM for its own processes, kernel buffers, and filesystem metadata. After reserving this, the remaining memory must be divided between PostgreSQL's shared_buffers, per-connection memory (work_mem and related), and the OS page cache.

A practical budget split for a 32GB system:
- OS reserved: ~2GB
- shared_buffers: 8GB (25% of total)
- Per-connection memory pool: ~6GB (work_mem, temp_buffers, maintenance)
- OS page cache: ~16GB (remainder, managed by the kernel)

The OS page cache is not wasted memory. PostgreSQL relies on it heavily for reading data not present in shared_buffers. Over-allocating to shared_buffers at the expense of the page cache can actually reduce performance.

## work_mem Pitfalls

work_mem is the most dangerous parameter to misconfigure. It is allocated per sort or hash operation, not per connection. A single complex query may use 3-5x work_mem if it involves multiple sorts, hash joins, or hash aggregations. The worst-case memory consumption is approximately:

```
max_connections x active_queries_per_conn x sorts_per_query x work_mem
```

Example: 100 connections, each running a query with 3 sort operations, with work_mem at 64MB = 100 x 3 x 64MB = 19.2GB. On a 32GB system, this triggers the OOM killer.

Safe approach: start with 4MB, monitor with `EXPLAIN (ANALYZE, BUFFERS)` for queries that spill to disk (indicated by "Sort Method: external merge"), then increase incrementally. For mixed workloads, set a conservative global default and use `SET LOCAL work_mem` in sessions that run analytical queries.

## maintenance_work_mem

Governs memory for VACUUM, CREATE INDEX, and ALTER TABLE ADD FOREIGN KEY. These operations benefit significantly from larger values (512MB-2GB). Since only a few maintenance operations typically run concurrently (autovacuum_max_workers defaults to 3), higher values are relatively safe. Note: autovacuum workers each consume up to autovacuum_work_mem (or maintenance_work_mem if autovacuum_work_mem is -1).

## temp_buffers

Controls the per-session cache for temporary tables. Default is 8MB. Increase if the workload creates large temporary tables (common in reporting and ETL pipelines). This memory is allocated lazily, only when temporary tables are accessed, so a higher setting has no cost for sessions that do not use temporary tables.

## Huge Pages

Linux huge pages (2MB vs the default 4KB page size) reduce TLB misses and page table overhead, which becomes significant when shared_buffers exceeds 8GB.

Configuration steps:
1. Set `huge_pages = try` in postgresql.conf
2. Calculate required huge pages: `shared_buffers / 2MB + small_margin`
3. Set `vm.nr_hugepages` in sysctl
4. Verify allocation in `/proc/meminfo` (HugePages_Free > 0)
5. Once confirmed working, change to `huge_pages = on` to fail at startup if huge pages are unavailable rather than silently falling back

## Monitoring Memory Usage

- **pg_stat_activity**: Shows current queries and their state. Use to estimate active connections and potential work_mem pressure.
- **pg_stat_bgwriter**: Tracks buffer allocation and checkpoint behaviour. High `buffers_alloc` relative to `buffers_backend` indicates healthy shared_buffers sizing.
- **OS tools**: `free -h` for overall memory distribution, `smem` for per-process proportional set size (PSS), `/proc/meminfo` for huge page status, `vmstat` for swap activity (any sustained swapping indicates over-allocation).

## Key Takeaway

Memory tuning in PostgreSQL requires reasoning about the multiplicative relationship between connections, concurrent operations, and per-operation memory. The safest strategy is to start conservative, monitor under realistic load, and increase parameters incrementally while watching for swap activity and OOM events.
