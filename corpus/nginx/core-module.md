---
source: "nginx Documentation: Core Module"
url: https://nginx.org/en/docs/ngx_core_module.html
service: "nginx"
section: core
license: "BSD-2-Clause"
retrieved: "2026-07-06"
---

# nginx Core Module Reference

The core module governs the fundamental behaviour of the nginx worker architecture, including process management, connection handling, and system-level tuning.

## worker_processes

Sets the number of worker processes. The recommended value is `auto`, which detects the number of available CPU cores and spawns one worker per core. Manual override is possible when CPU affinity or cgroup limits apply.

```nginx
worker_processes auto;
```

For containerised deployments where CPU quota differs from visible cores, set this explicitly to match the allocated CPU count.

## worker_connections

Defines the maximum number of simultaneous connections each worker process can handle. The default is 512. The effective maximum client capacity of the server is calculated as:

```
max_clients = worker_processes * worker_connections
```

For a server with 4 CPU cores and `worker_connections 1024`, the theoretical maximum is 4096 concurrent connections. When acting as a reverse proxy, each client request may consume two connections (one to the client, one to the upstream), so effective capacity is halved.

```nginx
events {
    worker_connections 1024;
}
```

## Event Model Selection

On Linux systems, the `epoll` event method should be used for efficient I/O multiplexing. It scales well with large numbers of connections compared to the older `select` or `poll` methods.

```nginx
events {
    use epoll;
}
```

On FreeBSD, use `kqueue` instead. Modern nginx versions auto-select the best available method, but explicit declaration ensures predictable behaviour.

## multi_accept

When enabled, a worker process accepts all pending connections at once rather than one at a time. This reduces latency under burst traffic.

```nginx
events {
    multi_accept on;
}
```

With `epoll` and `multi_accept on`, nginx processes incoming connections more efficiently during traffic spikes.

## worker_rlimit_nofile

Sets the maximum number of open file descriptors available to each worker process. This should be at least equal to `worker_connections`, and ideally higher to account for file serving, logging, and upstream connections. A common practice is to set it to double the `worker_connections` value.

```nginx
worker_rlimit_nofile 2048;
```

This directive changes the limit for the worker processes without requiring modification of system-level `ulimit` settings, which is useful when nginx is managed by a process supervisor that does not inherit shell limits.

## Practical Configuration Example

A typical production core configuration combining these directives:

```nginx
worker_processes auto;
worker_rlimit_nofile 65535;

events {
    use epoll;
    multi_accept on;
    worker_connections 16384;
}
```

## Key Considerations for Automated Provisioning

- Always prefer `worker_processes auto` unless operating within a constrained container environment with explicit CPU limits.
- Ensure `worker_rlimit_nofile` is set high enough to prevent "too many open files" errors under load.
- The `worker_connections` value should be tuned based on expected concurrency and whether nginx serves as a reverse proxy (which doubles the per-client connection cost).
- Verify the operating system's kernel-level limits (`fs.file-max`, `nofile` in systemd units) align with nginx's configured limits.
