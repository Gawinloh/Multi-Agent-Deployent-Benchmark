---
source: "Redis Documentation: Memory Optimization"
url: https://redis.io/docs/latest/operate/rs/references/memtier-benchmark/
service: "redis"
section: memory
license: "Redis Source Available License v2 / BSD-3-Clause"
retrieved: "2026-07-06"
---

# Redis Memory Management and Eviction Policies

## maxmemory Directive

The `maxmemory` configuration sets a hard upper limit on the amount of memory Redis can use for stored data. In production environments, this directive should always be explicitly configured. Without it, Redis will continue consuming memory until the operating system's out-of-memory (OOM) killer terminates the process, which can cause data loss and service disruption. The value can be specified in bytes or with human-readable suffixes such as `maxmemory 2gb`.

## maxmemory-policy Options

When Redis reaches the configured memory limit, the eviction policy determines how it handles new write requests. The available policies are:

- **noeviction** -- Returns an error on write commands when memory is full. No keys are removed. Suitable for use cases where Redis holds persistent or irreplaceable data and data loss is unacceptable.
- **allkeys-lru** -- Evicts the least recently used keys from the entire keyspace. The most common choice for general-purpose caching, where any key can be regenerated if needed.
- **volatile-lru** -- Evicts the least recently used keys only among those with an expiry (TTL) set. Keys without a TTL are never evicted, making this useful when some data must persist alongside cached data.
- **allkeys-lfu** -- Evicts the least frequently used keys from the entire keyspace. Preferable when access patterns are skewed and frequently accessed keys should be retained over rarely accessed ones.
- **volatile-lfu** -- Same as allkeys-lfu but restricted to keys with a TTL set.
- **allkeys-random** -- Randomly evicts keys from the entire keyspace. Appropriate when all keys have roughly equal importance and no access-pattern-based preference exists.
- **volatile-random** -- Randomly evicts keys that have a TTL set.
- **volatile-ttl** -- Evicts keys with the shortest remaining TTL first among those with an expiry. Useful when TTL values carry semantic meaning about data priority.

## Choosing the Right Policy

For typical caching workloads, **allkeys-lru** is the standard recommendation. It ensures that stale, unused data is removed first. When workloads exhibit power-law access patterns (a small subset of keys receives the majority of requests), **allkeys-lfu** provides better hit rates by preserving hot keys. For mixed workloads where some keys are persistent references and others are ephemeral cache entries, the **volatile-** variants allow Redis to evict only the expendable subset. Use **noeviction** when Redis serves as a primary data store rather than a cache, and data loss from eviction is not tolerable.

## How Redis Approximates LRU

Redis does not implement a true LRU algorithm, which would require significant memory overhead to maintain a linked list of all keys ordered by access time. Instead, it uses an approximated LRU approach: when eviction is needed, Redis samples a configurable number of keys at random and evicts the one with the oldest last-access timestamp among the sampled set. This approximation is memory-efficient and performs well in practice.

## maxmemory-samples

The `maxmemory-samples` parameter controls how many keys Redis samples during each eviction cycle. The default is 5, but setting it to **10** is recommended for a better balance between eviction accuracy and performance overhead. Higher values produce behavior closer to true LRU/LFU at the cost of slightly increased CPU usage per eviction. In most production scenarios, the performance difference between 5 and 10 samples is negligible, while the improvement in eviction accuracy is meaningful.
