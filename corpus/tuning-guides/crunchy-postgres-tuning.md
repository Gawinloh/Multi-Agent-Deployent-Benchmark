---
source: "Crunchy Data PostgreSQL Tuning Guide"
url: https://www.crunchydata.com/blog/postgresql-performance-tuning
service: "postgres"
section: tuning
license: "CC BY 4.0"
retrieved: "2026-07-06"
---

# Crunchy Data PostgreSQL Tuning Recommendations

## shared_buffers

Set to approximately 25% of total system RAM for most workloads. This is PostgreSQL's dedicated shared memory cache for table and index data. Going beyond 8GB rarely yields further benefit on typical OLTP systems because the OS page cache handles the remainder efficiently. On systems with 256GB+ RAM running large analytical queries, values up to 16GB may help, but benchmark before committing.

## effective_cache_size

Set to 50-75% of total RAM. This parameter does not allocate memory; it tells the query planner how much memory is available for caching between PostgreSQL's shared_buffers and the OS page cache. A higher value encourages index scans over sequential scans when the planner estimates that data is likely cached.

## work_mem

Start conservative (4-16MB) and increase based on query complexity. This memory is allocated per-sort-operation per-connection, so a query with multiple sorts or hash joins can consume several multiples of work_mem simultaneously. For analytics workloads with complex aggregations, values of 64-256MB may be appropriate, but only if max_connections is kept low.

## maintenance_work_mem

Controls memory for VACUUM, CREATE INDEX, and ALTER TABLE operations. Set to 512MB-2GB for large databases. Higher values speed up index creation and vacuum operations significantly. Unlike work_mem, only a few maintenance operations typically run concurrently, so larger values are safer.

## Checkpoint Tuning

- **max_wal_size**: Increase to 4-8GB for write-heavy workloads. The default (1GB) causes frequent checkpoints that generate I/O spikes. Larger values allow more WAL to accumulate between checkpoints, smoothing write I/O.
- **checkpoint_completion_target**: Set to 0.9 (spread checkpoint writes over 90% of the checkpoint interval). This prevents I/O storms by distributing dirty page flushes more evenly.
- **min_wal_size**: Set to 1-2GB to avoid repeated WAL file creation/deletion overhead.

## Connection Pooling

When max_connections exceeds 200, deploy PgBouncer or a similar connection pooler in front of PostgreSQL. Each PostgreSQL backend process consumes approximately 5-10MB of RAM. At 500+ connections, the overhead from context switching and lock contention degrades throughput substantially. Transaction-mode pooling in PgBouncer allows hundreds of application connections to share a smaller pool of actual database connections (typically 20-50).

## Storage and I/O Parameters

- **random_page_cost**: Set to 1.1-1.5 for SSD storage (default 4.0 assumes spinning disks). This corrects the planner's cost model so it favours index scans appropriately on fast storage.
- **effective_io_concurrency**: Set to 200 for SSD-backed storage. This allows PostgreSQL to issue multiple concurrent I/O requests during bitmap heap scans and prefetching, exploiting SSD parallelism. Leave at 1 for spinning disks.
- **seq_page_cost**: Usually left at 1.0 but can be reduced to 0.1 on very fast NVMe storage relative to random_page_cost.

## WAL Configuration

- **wal_buffers**: Set to 64MB for write-heavy workloads (auto-sized to 1/32 of shared_buffers by default).
- **wal_compression**: Enable on CPU-rich, I/O-constrained systems to reduce WAL volume.

## Summary

The most impactful parameters for initial tuning are shared_buffers, effective_cache_size, work_mem, and random_page_cost. Checkpoint tuning and connection pooling become critical as write volume and concurrency increase. Always benchmark changes with realistic workloads before deploying to production.
