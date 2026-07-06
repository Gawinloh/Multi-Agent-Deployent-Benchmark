---
source: "Redis Documentation: LRU/LFU Eviction"
url: https://redis.io/docs/latest/develop/reference/eviction/
service: "redis"
section: eviction-tuning
license: "Redis Source Available License v2 / BSD-3-Clause"
retrieved: "2026-07-06"
---

# Redis LRU and LFU Eviction: Tuning Guide

## Approximated LRU in Redis

Redis does not implement a classical LRU algorithm. A true LRU would require maintaining a doubly-linked list of every key ordered by last access time, consuming substantial additional memory per key. Instead, Redis uses a sampling-based approximation: when an eviction is needed, it randomly selects N keys (controlled by `maxmemory-samples`) and evicts the one among those samples that was accessed least recently.

Each key stores its last access timestamp in a 24-bit field within the key's internal object header, which limits the time resolution but keeps per-key overhead minimal. The approximation works well in practice because even a small random sample reliably identifies stale keys in typical workloads. As the sample size increases, eviction decisions approach the accuracy of a true LRU implementation.

## LFU Eviction (Redis 4.0+)

Least Frequently Used eviction was introduced in Redis 4.0 as an alternative to LRU. While LRU tracks when a key was last accessed, LFU tracks how often a key is accessed, using a probabilistic frequency counter known as a Morris counter. This 8-bit counter provides a logarithmic approximation of access frequency, keeping memory overhead extremely low.

LFU eviction is governed by two tuning parameters:

### lfu-log-factor

Controls how quickly the frequency counter saturates. A higher value means more accesses are needed to reach the counter's maximum value. The default is `10`, which causes the counter to saturate at roughly one million accesses. Lower values cause faster saturation (less granularity between moderately and heavily accessed keys), while higher values provide more differentiation but require more accesses to register significance.

### lfu-decay-time

Specifies the number of minutes after which the frequency counter is halved if the key has not been accessed. The default is `1` minute. This decay mechanism ensures that keys which were popular in the past but are no longer accessed will eventually be evicted. Setting this to `0` causes the counter to decay every time it is scanned, making eviction very aggressive toward previously popular but currently idle keys.

## When to Use LRU vs LFU

**LRU is preferable** when access patterns are temporally clustered -- that is, recently accessed keys are likely to be accessed again soon, and the working set shifts over time. It works well for session caches, time-series data, and workloads where recency is a strong predictor of future access.

**LFU is preferable** when access patterns follow a power-law distribution, where a small subset of keys receives the vast majority of requests. In such workloads, LRU may evict popular keys simply because they were not accessed in the most recent sampling window, while LFU retains them based on their accumulated access frequency. Common examples include product catalogues, user profile lookups, and content delivery caches.

## Tuning maxmemory-samples

The `maxmemory-samples` parameter directly affects eviction accuracy for both LRU and LFU policies. The default value of `5` provides reasonable performance with minimal CPU overhead. Increasing this to `10` substantially improves eviction accuracy with negligible performance impact in most scenarios. Values above 10 offer diminishing returns. The Redis documentation includes visual comparisons showing that a sample size of 10 produces eviction patterns nearly indistinguishable from a true LRU implementation.

## Monitoring Eviction Behaviour

Use `INFO stats` to monitor the `evicted_keys` metric, which reports the total number of keys evicted due to memory pressure since the server started. A steadily increasing eviction rate may indicate that `maxmemory` is too low for the workload, or that the eviction policy is not well-matched to the access pattern. Pair this metric with `keyspace_hits` and `keyspace_misses` to evaluate cache effectiveness -- a high miss rate alongside frequent evictions suggests the working set exceeds available memory.
