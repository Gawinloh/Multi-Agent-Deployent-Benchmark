---
source: "PostgreSQL 16 Documentation: Client Authentication"
url: https://www.postgresql.org/docs/16/auth-pg-hba-conf.html
service: "postgres"
section: authentication
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# Client Authentication: pg_hba.conf

The pg_hba.conf file (host-based authentication) controls which clients can connect to which databases, as which users, and what authentication method is required. It is read at server start and when the server receives a SIGHUP signal or `pg_ctl reload` is executed.

## Record Format

Each line in pg_hba.conf is a record with the following fields:

```
TYPE  DATABASE  USER  ADDRESS  METHOD  [OPTIONS]
```

- **TYPE**: The connection type. Common values are `local` (Unix-domain socket), `host` (TCP/IP with or without SSL), `hostssl` (TCP/IP with SSL required), and `hostnossl` (TCP/IP without SSL).
- **DATABASE**: The target database name. Use `all` to match any database, `sameuser` to match a database with the same name as the connecting user, or `replication` for replication connections. Multiple database names can be comma-separated.
- **USER**: The connecting user name. Use `all` to match any user. Multiple user names can be comma-separated. Group names can be prefixed with `+`.
- **ADDRESS**: The client IP address or CIDR range for `host`, `hostssl`, and `hostnossl` records. Not used for `local` records. Examples: `192.168.1.0/24`, `0.0.0.0/0` (all IPv4), `::0/0` (all IPv6).
- **METHOD**: The authentication method to apply.

## Authentication Methods

- **trust**: Allows the connection unconditionally without requiring a password. Should never be used for remote connections in production. Acceptable only for local development environments.
- **reject**: Unconditionally rejects the connection. Useful for explicitly blocking specific users, databases, or address ranges.
- **scram-sha-256**: Requires SCRAM-SHA-256 password authentication. This is the recommended method for password-based authentication. It uses salted challenge-response and is resistant to replay and offline attacks.
- **md5**: Requires MD5-hashed password authentication. Weaker than SCRAM-SHA-256 and retained primarily for backward compatibility. The password hash depends on the username, so renaming a user invalidates the stored hash.
- **peer**: Authenticates by obtaining the client's OS username from the kernel and comparing it to the requested database username. Only available for local connections. Commonly used so that the `postgres` OS user can connect as the `postgres` database superuser without a password.
- **cert**: Authenticates using SSL client certificates. Requires that the client present a valid certificate signed by the server's trusted CA.
- **ldap**: Authenticates against an external LDAP directory server.
- **gss**: Authenticates using GSSAPI (Kerberos).

## Record Order

Records are evaluated from top to bottom. The first matching record determines the authentication outcome. There is no fall-through: once a match is found, subsequent records are ignored. This means more specific rules must appear before more general ones.

## Recommended Patterns

- Place `local all postgres peer` near the top to allow the OS postgres user to connect locally.
- Use `hostssl all all 0.0.0.0/0 scram-sha-256` for remote connections to enforce both encryption and strong authentication.
- Never use `trust` for `host` connections from external networks.
- Add explicit `reject` rules before broad `all` rules to block known-bad sources or restrict specific databases.
- Require SSL for all remote connections by using `hostssl` instead of `host`.

## Reloading

After editing pg_hba.conf, the configuration can be reloaded without restarting the server using `SELECT pg_reload_conf();` or `pg_ctl reload`. Existing connections are not affected; only new connections use the updated rules.
