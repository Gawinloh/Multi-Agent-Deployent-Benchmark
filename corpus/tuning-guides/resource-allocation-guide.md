---
source: "Resource allocation heuristics for single-host stacks (authored)"
url: "N/A — authored for this project"
section: resource-allocation
license: "CC0"
retrieved: "2026-07-06"
---

# Resource Allocation Guide for Single-Host Stacks

This guide provides sizing heuristics for a single machine running nginx, PostgreSQL, and Redis under Docker Compose. Allocations are expressed as percentages of total host RAM and CPU cores, parameterised by workload class.

## Workload Classes

### Web/API Workload (Default)

The most common pattern: a web application serving HTTP requests backed by PostgreSQL for persistence and Redis for session/cache.

**RAM allocation (total host RAM):**
- PostgreSQL: 60% of total RAM
  - shared_buffers: 15% of total RAM (25% of PG's allocation)
  - Remainder available for work_mem, maintenance_work_mem, OS page cache for PG data files
- Redis: 15% of total RAM (set as maxmemory)
- nginx: minimal, ~128MB for proxy buffers and connection state
- OS reserve: 15-20% for kernel, filesystem cache, container overhead, headroom

**CPU allocation:**
- PostgreSQL: 60% of available cores (use cpus in Docker Compose)
- nginx: worker_processes set to remaining cores, minimum 2
- Redis: 1 core (single-threaded for command processing; io-threads can use more for I/O)

### Cache-Heavy Workload

Applications where Redis serves as the primary data layer (large session stores, real-time leaderboards, feature flags at scale).

**RAM allocation:**
- Redis: 40% of total RAM (maxmemory)
- PostgreSQL: 35% of total RAM
  - shared_buffers: ~9% of total RAM
- nginx: minimal (~128MB)
- OS reserve: 20%

**CPU allocation:**
- Redis: 2 cores (1 main + io-threads)
- PostgreSQL: 50% of remaining cores
- nginx: remaining cores (minimum 2)

### Analytics/Data Warehouse Workload

Complex queries, large aggregations, batch imports, and reporting. Few concurrent connections but high per-query resource demand.

**RAM allocation:**
- PostgreSQL: 70% of total RAM
  - shared_buffers: 17-18% of total RAM
  - work_mem: 64-256MB (safe because max_connections is typically low, 20-50)
  - maintenance_work_mem: 1-2GB (large index builds, VACUUM on big tables)
- Redis: 10% of total RAM (minimal caching role)
- nginx: minimal (~128MB)
- OS reserve: 15%

**CPU allocation:**
- PostgreSQL: 80% of cores (max_parallel_workers_per_gather = cores/4)
- nginx: 1-2 workers (low HTTP concurrency)
- Redis: 1 core

## General Rules

### Never Exceed 80% Total RAM Allocation

The sum of all service memory limits must not exceed 80% of total host RAM. The remaining 20% provides headroom for:
- OS page cache (critical for PostgreSQL performance)
- Kernel memory management structures
- Container runtime overhead (Docker daemon, cgroups accounting)
- Traffic spikes and transient memory pressure

### work_mem Budget Constraint

The product `max_connections x average_sorts_per_query x work_mem` must fit within the PostgreSQL memory budget. For a Web/API workload on a 16GB host with PG budget of 9.6GB:
- shared_buffers: 2.4GB
- Remaining for connections: ~7.2GB
- At 100 connections, 2 sorts each: work_mem <= 36MB
- At 20 connections (pooled), 2 sorts each: work_mem <= 180MB

Connection pooling (PgBouncer) dramatically increases the safe work_mem ceiling.

### Redis maxmemory Must Be Hard-Capped

Always set maxmemory explicitly in Redis configuration. Without it, Redis grows unbounded and will be killed by the Linux OOM killer, potentially corrupting AOF/RDB persistence. Set the eviction policy (maxmemory-policy) based on use case:
- **allkeys-lru**: general caching (evict least recently used keys)
- **volatile-lru**: only evict keys with TTL set (preserves persistent keys)
- **noeviction**: session stores where data loss is unacceptable (returns errors when full)

### Docker Compose Memory Limits

Always set both `mem_limit` and `memswap_limit` (equal to mem_limit to disable swap) in Docker Compose:

```yaml
services:
  postgres:
    mem_limit: 9600m
    memswap_limit: 9600m
  redis:
    mem_limit: 2400m
    memswap_limit: 2400m
```

This ensures the OOM killer targets the correct container rather than random host processes.
