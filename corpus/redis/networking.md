---
source: "Redis Documentation: redis.conf"
url: https://redis.io/docs/latest/operate/oss_and_stack/management/config/
service: "redis"
section: networking
license: "Redis Source Available License v2 / BSD-3-Clause"
retrieved: "2026-07-06"
---

# Redis Networking Configuration

Redis networking directives control how the server accepts and manages client connections. Proper configuration is important for both performance and security in production environments.

## bind

The `bind` directive specifies which network interfaces Redis listens on. By default, Redis binds to `127.0.0.1 ::1` (localhost only). Setting `bind 0.0.0.0` causes Redis to listen on all available interfaces, which exposes the instance to any reachable network. In production, avoid binding to all interfaces unless Redis is placed behind a firewall or within a private network segment. Instead, bind to the specific internal IP address that application servers connect through.

Multiple addresses can be specified: `bind 192.168.1.10 127.0.0.1` makes Redis reachable on both a private network interface and localhost.

## port

The default listening port is `6379`. This can be changed with the `port` directive. Setting `port 0` disables TCP listening entirely, which is useful when Redis should only accept connections through a Unix socket.

## timeout

The `timeout` directive controls how many seconds a client connection can remain idle before Redis closes it. The default value of `0` means connections are never closed due to inactivity. In production, setting a timeout (e.g., `timeout 300`) helps reclaim resources from abandoned or leaked connections. Choose a value that exceeds the longest expected idle period for legitimate clients.

## tcp-backlog

The `tcp-backlog` setting defines the size of the TCP listen backlog queue. The default is `511`. For deployments with high connection rates (such as applications that frequently open and close connections), increasing this value (e.g., `tcp-backlog 2048`) prevents connection refusals during bursts. Note that the effective backlog is also limited by the operating system's `somaxconn` and `tcp_max_syn_backlog` kernel parameters, which may need to be increased as well.

## tcp-keepalive

The `tcp-keepalive` directive sets the interval in seconds at which Redis sends TCP keepalive probes on idle connections. The recommended value is `300` seconds. Keepalive probes serve two purposes: they detect dead peers (clients that disconnected without closing the connection properly) and they prevent intermediate network devices such as firewalls and load balancers from dropping idle connections due to inactivity timeouts. Setting this to `0` disables keepalive probes.

## Unix Socket

For applications running on the same host as Redis, Unix domain sockets provide lower latency and higher throughput than TCP by bypassing the network stack entirely. Configuration requires two directives:

```
unixsocket /var/run/redis/redis.sock
unixsocketperm 770
```

The `unixsocket` directive specifies the socket file path, and `unixsocketperm` sets its file permissions. Restrict permissions so that only the application user and the Redis group can connect. Unix sockets and TCP can be used simultaneously, allowing both local and remote clients to connect.

## maxclients

The `maxclients` directive sets the maximum number of simultaneous client connections Redis will accept. The default is `10000`. When this limit is reached, new connections are refused with an error. The actual achievable limit may be constrained by the operating system's file descriptor limit, since each connection requires a file descriptor. Redis attempts to reserve 32 file descriptors for internal use. If the OS limit is lower than `maxclients + 32`, Redis adjusts the maximum accordingly and logs a warning at startup. For high-connection deployments, ensure that the system's `ulimit -n` value is set sufficiently high.
