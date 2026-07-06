---
source: "PostgreSQL 16 Documentation: Resource Consumption"
url: https://www.postgresql.org/docs/16/runtime-config-resource.html
service: "postgres"
section: memory
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# Resource Consumption

PostgreSQL provides several parameters that control memory allocation and resource usage. Proper tuning of these values is critical for performance and stability.

## shared_buffers

Controls the amount of memory the server uses for shared memory buffers. The default is typically 128 MB. The recommended starting point is 25% of total system RAM. On systems with more than 32 GB of RAM, diminishing returns begin, and values above 40% of RAM rarely provide additional benefit. The minimum value is 128 kB. Changes to this parameter require a server restart.

Larger values allow PostgreSQL to cache more data in memory, reducing disk I/O. However, PostgreSQL also relies on the operating system's file cache, so allocating too much to shared_buffers can starve the OS cache and degrade overall performance.

## work_mem

Specifies the amount of memory used by internal sort operations and hash tables before writing to temporary disk files. The default is 4 MB. This value is per-operation, not per-connection -- a single complex query may perform multiple sort or hash operations simultaneously, each consuming up to work_mem. The effective memory consumption is therefore work_mem multiplied by the number of concurrent operations across all active connections.

Setting this too high can cause out-of-memory conditions under heavy concurrency. Setting it too low forces frequent disk-based sorting, degrading query performance. A common approach is to calculate (available_RAM - shared_buffers) / (max_connections * 3) as a starting point.

## maintenance_work_mem

Controls the maximum amount of memory used by maintenance operations such as VACUUM, CREATE INDEX, and ALTER TABLE ADD FOREIGN KEY. The default is 64 MB. Because these operations typically run one at a time, it is generally safe to set this significantly higher than work_mem -- values of 512 MB to 2 GB are common on systems with sufficient RAM. Higher values improve the speed of VACUUM and index creation.

The autovacuum_work_mem parameter can be set separately if autovacuum workers need a different allocation from manual maintenance operations. If not set, autovacuum inherits from maintenance_work_mem.

## effective_cache_size

This parameter does not allocate any memory. It is a hint to the query planner indicating how much memory is expected to be available for disk caching by the operating system and within PostgreSQL's shared buffers combined. The planner uses this to estimate the likelihood of finding data in cache versus needing disk I/O, which influences its choice between sequential and index scans.

A typical setting is 50-75% of total system RAM. Setting it too low causes the planner to avoid index scans when they would be beneficial. Setting it too high may lead the planner to prefer index scans on data that is actually not cached, causing random I/O.

## huge_pages

Controls whether huge pages are requested from the operating system for shared memory allocation. Possible values are `try` (the default), `on`, and `off`. Huge pages reduce translation lookaside buffer (TLB) pressure and can improve performance, particularly on systems with large shared_buffers allocations (multiple gigabytes).

When set to `try`, PostgreSQL attempts to use huge pages but falls back to normal pages if they are unavailable. When set to `on`, failure to allocate huge pages prevents the server from starting. The operating system must be configured to provide huge pages (on Linux, via `vm.nr_hugepages` in sysctl).

## temp_buffers

Sets the maximum number of temporary buffers used by each session for accessing temporary tables. The default is 8 MB. This can be changed within individual sessions but only before first use of temporary tables in that session.

## temp_file_limit

Limits the total size of temporary files that a single process can create. The default of -1 means no limit. Setting a positive value prevents runaway queries from filling the disk with temporary data.
