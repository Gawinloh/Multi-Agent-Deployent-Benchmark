---
source: "CIS Redis Benchmark v1.0 (paraphrased extracts)"
url: https://www.cisecurity.org/benchmark/redis
service: redis
license: "CIS Terms of Use (non-commercial research)"
retrieved: "2026-07-06"
---

# CIS Redis — Level 1 Controls (Implemented)

The following controls are paraphrased summaries of CIS Benchmark recommendations relevant to the project's Redis validator. Only controls that the validator checks are included.

## Network and Access

### CIS Redis — Protected mode must be enabled

The `protected-mode` setting prevents Redis from accepting connections from external interfaces when no authentication is configured. With protected mode disabled, an unsecured Redis instance bound to a public interface is fully accessible to any network client. Set `protected-mode yes` in `redis.conf` to ensure Redis rejects external connections unless authentication has been explicitly configured.

### CIS Redis — Bind address must not be wildcard

Binding Redis to `0.0.0.0` or `*` causes it to listen on all network interfaces, exposing the service to untrusted networks. An internet-reachable Redis instance with weak or no authentication is a common vector for data theft and remote code execution. Set `bind` to specific internal IP addresses (e.g., `bind 127.0.0.1` or the private subnet address) to restrict network exposure.

## Authentication

### CIS Redis — Authentication must be configured

Without authentication, any client that can reach the Redis port has full read/write access to all data. This allows trivial data exfiltration and can enable remote code execution through commands like `MODULE LOAD`. Set `requirepass` to a strong password in `redis.conf`, or configure ACL users with individual credentials and fine-grained command permissions using the `user` directive.

### CIS Redis — Unauthenticated PING must be rejected

By default, Redis responds to `PING` from unauthenticated clients, confirming that the service is running and reachable. This information aids attackers in reconnaissance and port scanning. Verify at runtime that sending `PING` without authenticating returns an authentication error rather than `PONG`, confirming that the auth requirement is enforced before any command processing.

## Command Restriction

### CIS Redis — Dangerous commands must be renamed or disabled

Commands such as `FLUSHALL`, `FLUSHDB`, `CONFIG`, `EVAL`, `DEBUG`, and `SHUTDOWN` can destroy data, alter server configuration, or execute arbitrary scripts. If an attacker gains access, these commands enable immediate and complete compromise. Use the `rename-command` directive in `redis.conf` to map each dangerous command to an empty string (disabling it) or to a secret alias known only to administrators.

## Durability

### CIS Redis — Data persistence must be configured

Without persistence, all data is lost if the Redis process restarts or the host reboots. For workloads where data loss is unacceptable, at least one persistence mechanism must be active. Enable append-only file logging with `appendonly yes`, or configure RDB snapshots with one or more `save` directives (e.g., `save 900 1`) to ensure data survives process restarts.

## Memory Management

### CIS Redis — Maximum memory limit must be set

Without a `maxmemory` limit, Redis grows its memory usage without bound until the operating system kills the process or the host becomes unresponsive. This affects all co-located services and can cause cascading failures. Set `maxmemory` to an appropriate byte value (e.g., `maxmemory 256mb`) that leaves headroom for the operating system and other processes.

### CIS Redis — Eviction policy must not be noeviction for cache workloads

When `maxmemory-policy` is set to `noeviction`, Redis returns errors on write commands once the memory limit is reached rather than removing older keys. For cache use cases this causes application failures when the cache is full. Set `maxmemory-policy` to an appropriate eviction strategy such as `allkeys-lru` or `volatile-lru` so that Redis can automatically reclaim memory by removing least-recently-used keys.
