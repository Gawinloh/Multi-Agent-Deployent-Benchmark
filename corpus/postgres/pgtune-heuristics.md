---
source: "pgtune heuristics (community rules)"
url: https://pgtune.leopard.in.ua/
service: "postgres"
section: tuning
license: "MIT (pgtune)"
retrieved: "2026-07-06"
---

# PGTune Heuristics

PGTune is a community tool that generates PostgreSQL configuration recommendations based on hardware specifications and workload type. The following heuristics represent widely adopted tuning rules derived from pgtune (pgtune.leopard.in.ua) and the PostgreSQL wiki tuning guidance.

## Workload Classes

PGTune categorizes workloads into four classes, each with different parameter profiles:

- **Web**: High connection count, many short transactions. Typical max_connections: 200.
- **OLTP**: Online transaction processing with moderate connections. Typical max_connections: 300.
- **Data Warehouse (DW)**: Few connections, complex analytical queries. Typical max_connections: 40.
- **Mixed**: General-purpose workload. Typical max_connections: 100.

## Memory Parameters

### shared_buffers

Set to 25% of total system RAM as a starting point. On systems with very large amounts of RAM (64 GB or more), cap shared_buffers at approximately 8 GB, as the benefit plateaus and additional memory is better utilized by the OS page cache.

### effective_cache_size

Set to approximately 75% of total system RAM. This is not a memory allocation but a planner hint indicating how much data is expected to reside in the combined PostgreSQL shared buffers and OS file cache.

### maintenance_work_mem

Set to total RAM divided by 16. Cap at 2 GB. This parameter governs memory for VACUUM, CREATE INDEX, and similar maintenance operations. Higher values accelerate these operations but provide no benefit beyond 2 GB.

### work_mem

Calculate as (total RAM - shared_buffers) / (max_connections * 3). The multiplier of 3 accounts for the fact that a single query may perform multiple sort or hash operations concurrently. For data warehouse workloads with few connections, this formula yields larger per-operation memory, which is appropriate for complex analytical queries.

Example for a 16 GB system with 200 connections (web workload):
- shared_buffers = 4 GB
- work_mem = (16 GB - 4 GB) / (200 * 3) = 20 MB

Example for a 16 GB system with 40 connections (DW workload):
- shared_buffers = 4 GB
- work_mem = (16 GB - 4 GB) / (40 * 3) = 100 MB

## WAL and Checkpoint Parameters

### checkpoint_completion_target

Set to 0.9 for all workload types. This spreads checkpoint writes over 90% of the checkpoint interval, smoothing I/O load.

### max_wal_size

Varies by workload:
- Web: 2 GB
- OLTP: 4 GB
- Data Warehouse: 8 GB (higher write volumes from bulk loads)
- Mixed: 2 GB

### min_wal_size

Typically set to 1 GB for DW workloads and 512 MB-1 GB for others, ensuring sufficient recycled WAL segments are available for write bursts.

## Storage Parameters

### random_page_cost

Set to 1.1 for SSD-based storage, down from the default of 4.0 (which assumes spinning disks). This tells the query planner that random reads are nearly as fast as sequential reads on SSDs, encouraging the use of index scans where appropriate.

### effective_io_concurrency

Set to 200 for SSD-based storage (default is 1). This allows PostgreSQL to issue multiple concurrent I/O requests, taking advantage of SSD parallelism during bitmap heap scans and other operations.

## Additional Recommendations

### wal_buffers

PGTune typically sets this to 16 MB for systems with shared_buffers of 512 MB or more, matching the auto-tuned maximum. For smaller configurations, the default auto-calculated value (1/32 of shared_buffers) is usually sufficient.

### default_statistics_target

Increasing from the default of 100 to 200 or 500 for DW workloads can improve query plans on large tables by collecting more detailed column statistics during ANALYZE.

## Sources

These heuristics are documented by the PGTune project at pgtune.leopard.in.ua and are consistent with the tuning recommendations in the PostgreSQL wiki (wiki.postgresql.org/wiki/Tuning_Your_PostgreSQL_Server).
