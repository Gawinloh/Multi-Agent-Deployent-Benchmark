---
source: "Redis Documentation: Security"
url: https://redis.io/docs/latest/operate/oss_and_stack/management/security/
service: "redis"
section: security
license: "Redis Source Available License v2 / BSD-3-Clause"
retrieved: "2026-07-06"
---

# Redis Security Configuration

Redis was originally designed for trusted environments, so security hardening is essential for any deployment exposed beyond localhost. The following directives and features control access, authentication, and encryption.

## protected-mode

Enabled by default (`protected-mode yes`), this safeguard prevents Redis from accepting connections from external network interfaces when no authentication is configured. If Redis is bound to all interfaces but has no password set, it will only serve clients connecting from the loopback address. This acts as a safety net against accidental exposure but should not be relied upon as the sole security measure.

## bind Directive

The `bind` directive restricts which network interfaces Redis listens on. The default configuration is `bind 127.0.0.1 ::1`, which limits connections to localhost on both IPv4 and IPv6. In production, bind Redis to the specific internal network interface that application servers use. Avoid `bind 0.0.0.0` unless Redis is behind a firewall and other access controls are in place.

## Authentication with requirepass

The `requirepass` directive sets a server-wide password that clients must supply via the `AUTH` command before executing any other commands. Set a strong, long password (Redis can handle passwords of arbitrary length, and a longer password provides better protection against brute-force attacks). Note that `requirepass` alone provides a single shared credential with full access to all commands and data.

## Access Control Lists (ACLs)

Introduced in Redis 6, ACLs provide fine-grained, per-user access control. Users are created and configured with `ACL SETUSER`, which supports:

- **Command restrictions** -- Allow or deny specific commands or command categories (e.g., `+get +set -flushall` or `+@read -@dangerous`).
- **Key patterns** -- Restrict which keys a user can access using glob-style patterns (e.g., `~cache:*` limits access to keys prefixed with `cache:`).
- **Pub/Sub channel patterns** -- Control which channels a user can subscribe to or publish on.
- **Password management** -- Each user can have one or more passwords, and passwords can be rotated without downtime.

ACLs replace the single-password model with role-based access, which is important for multi-tenant deployments or when different application components need different permission levels.

## rename-command

Dangerous commands can be disabled or obscured by renaming them in the configuration file. This is especially important for commands that can cause data loss or expose server internals:

```
rename-command FLUSHALL ""
rename-command FLUSHDB ""
rename-command CONFIG "CONFIG_a8f3b2e9"
rename-command DEBUG ""
rename-command SHUTDOWN "SHUTDOWN_c4d7e1f0"
rename-command EVAL ""
```

Setting a command name to an empty string disables it entirely. Renaming to a random string allows authorised administrators to use the command while preventing accidental or malicious invocation. Note that renamed commands are recorded with their new names in the AOF, which can cause issues if the configuration changes between persistence and reload.

## TLS/SSL Encryption

Redis supports TLS for encrypting client-server and replication traffic. Key configuration directives include:

- `tls-port 6380` -- The port on which Redis accepts TLS connections (typically alongside or instead of the unencrypted `port`).
- `tls-cert-file /path/to/redis.crt` -- The server certificate file.
- `tls-key-file /path/to/redis.key` -- The private key file.
- `tls-ca-cert-file /path/to/ca.crt` -- The CA certificate for verifying client certificates in mutual TLS configurations.

Enabling TLS is recommended for any deployment where Redis traffic traverses untrusted networks or where compliance requirements mandate encryption in transit.
