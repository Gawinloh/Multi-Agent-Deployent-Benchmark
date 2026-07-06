---
source: "PostgreSQL 16 Documentation: Connections and Authentication"
url: https://www.postgresql.org/docs/16/runtime-config-connection.html
service: "postgres"
section: connections
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# Connections and Authentication

These parameters control how clients connect to the PostgreSQL server and how authentication is handled at the server configuration level.

## listen_addresses

Specifies the TCP/IP addresses on which the server listens for client connections. The default is `localhost`, meaning only local connections via the loopback interface are accepted. Setting this to `*` allows connections on all available network interfaces. Multiple addresses can be specified as a comma-separated list (e.g., `'192.168.1.10, 10.0.0.5'`). An empty string disables TCP/IP connections entirely, limiting access to Unix-domain sockets only.

This parameter can only be set at server start. Changing it requires a restart.

## max_connections

Determines the maximum number of concurrent connections to the database server. The default is 100. Each connection consumes approximately 10 MB of memory (including shared memory and per-connection process overhead), so a server with 200 connections requires roughly 2 GB just for connection overhead.

Setting this too high on a system with limited RAM can cause memory pressure. Connection poolers such as PgBouncer are recommended for workloads requiring many client connections, as they multiplex many client connections onto fewer server connections.

This parameter requires a server restart to take effect.

## superuser_reserved_connections

Reserves a specified number of connection slots for superuser logins. The default is 3. These slots are drawn from the max_connections total -- they are not additional connections. This ensures that administrators can always connect for maintenance even when normal connection slots are exhausted.

The effective number of connections available to non-superuser roles is max_connections minus superuser_reserved_connections.

## password_encryption

Specifies the algorithm used to encrypt passwords when a password is set via CREATE ROLE or ALTER ROLE. The supported values are `scram-sha-256` and `md5`. The default in PostgreSQL 16 is `scram-sha-256`.

SCRAM-SHA-256 is strongly recommended over MD5. MD5 password hashing has known cryptographic weaknesses: the hash incorporates the username, meaning that if a user is renamed, the password hash becomes invalid; and the hash itself is vulnerable to offline brute-force attacks. SCRAM-SHA-256 uses salted iterated hashing and provides challenge-response authentication, preventing password replay.

When migrating from MD5 to SCRAM-SHA-256, all existing user passwords must be reset after changing this parameter, since the stored hash format differs.

## authentication_timeout

Specifies the maximum time allowed for a client to complete authentication after connecting. The default is 60 seconds (1 minute). If authentication is not completed within this period, the server closes the connection. This prevents half-open connections from consuming resources indefinitely.

This parameter is set in postgresql.conf and applies globally. It cannot be changed per-user or per-database.

## tcp_keepalives_idle, tcp_keepalives_interval, tcp_keepalives_count

These parameters control TCP keepalive behavior for client connections. Setting appropriate keepalive values helps detect dead connections caused by network failures, firewalls dropping idle connections, or client crashes. Typical values are idle=60, interval=10, count=6, meaning that after 60 seconds of inactivity, a keepalive probe is sent every 10 seconds, and the connection is dropped after 6 failed probes.

## client_connection_check_interval

Introduced in PostgreSQL 14, this parameter enables periodic checking of whether the client connection is still alive during long-running queries. The default is 0 (disabled). Setting it to a positive value (in milliseconds) allows the server to detect client disconnections and cancel orphaned queries that would otherwise run to completion.
