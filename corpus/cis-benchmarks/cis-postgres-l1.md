---
source: "CIS PostgreSQL 16 Benchmark v1.0 (paraphrased extracts)"
url: https://www.cisecurity.org/benchmark/postgresql
service: postgres
license: "CIS Terms of Use (non-commercial research)"
retrieved: "2026-07-06"
---

# CIS PostgreSQL 16 — Level 1 Controls (Implemented)

The following controls are paraphrased summaries of CIS Benchmark recommendations relevant to the project's PostgreSQL validator. Only controls that the validator checks are included.

## 1.x — Authentication

### CIS PG 1.x — Password encryption must use SCRAM-SHA-256

The `password_encryption` parameter controls the hash algorithm used when storing role passwords. MD5 hashing is considered cryptographically weak and vulnerable to offline brute-force attacks. Set `password_encryption = 'scram-sha-256'` in `postgresql.conf` to ensure all newly created or altered passwords use the stronger SCRAM mechanism.

### CIS PG 1.x — pg_hba.conf must enforce SCRAM-SHA-256 for host connections

Client authentication rules in `pg_hba.conf` determine which authentication method is required for each connection type. Allowing `md5` as an auth method means clients can authenticate with the weaker hash, even if passwords are stored as SCRAM hashes. All `host`, `hostssl`, and `hostnossl` entries should specify `scram-sha-256` as the authentication method, and no lines should use `md5`.

## 2.x — Logging and Auditing

### CIS PG 2.x — log_destination must be configured

The `log_destination` parameter determines where PostgreSQL sends its log output (e.g., `stderr`, `csvlog`, `syslog`). Without an explicit destination, log data may be lost or written to an inaccessible location, undermining incident investigation. Set `log_destination` to an appropriate value such as `'stderr'` or `'csvlog'` to ensure logs are captured reliably.

### CIS PG 2.x — log_statement should be at least 'ddl'

The `log_statement` setting controls which SQL statements are recorded in the server log. Logging DDL statements (CREATE, ALTER, DROP) provides an audit trail of schema changes that could indicate unauthorised modification. Set `log_statement = 'ddl'` or `'all'` to capture structural changes to the database.

### CIS PG 2.x — log_connections must be enabled

When `log_connections` is off, successful and failed connection attempts are not recorded, making it difficult to detect brute-force attacks or unauthorised access. Enabling this parameter logs each connection attempt including the username, database, and source address. Set `log_connections = on` in `postgresql.conf`.

### CIS PG 2.x — log_disconnections must be enabled

The `log_disconnections` parameter records when each session ends and its duration. Without this, administrators cannot correlate session lifetimes with suspicious activity or diagnose connection leaks. Set `log_disconnections = on` to maintain a complete session lifecycle audit trail.

### CIS PG 2.x — pgAudit extension must be installed and active

The pgAudit extension provides detailed session and object-level audit logging beyond what native PostgreSQL logging offers. Without it, fine-grained tracking of SELECT queries and function calls is unavailable. Add `pgaudit` to `shared_preload_libraries` in `postgresql.conf` and run `CREATE EXTENSION pgaudit` in each database that requires auditing.

## 3.x — Network and Transport

### CIS PG 3.x — listen_addresses must not be wildcard

Setting `listen_addresses = '*'` causes PostgreSQL to accept connections on every network interface, including public-facing ones. This unnecessarily increases the attack surface by exposing the database port to networks that should not have direct access. Set `listen_addresses` to specific IP addresses or hostnames that correspond to trusted networks only.

### CIS PG 3.x — SSL must be enabled

When SSL is disabled, all client-server communication travels in plaintext, exposing credentials and query data to network eavesdropping. Enabling SSL encrypts the connection and allows mutual certificate verification. Set `ssl = on` in `postgresql.conf` and provide valid certificate and key files.

### CIS PG 3.x — Minimum TLS protocol version must be 1.2 or higher

Older TLS versions (1.0 and 1.1) contain known vulnerabilities such as BEAST and POODLE that allow attackers to decrypt traffic. Enforcing TLS 1.2 as the minimum version ensures only modern, secure protocol negotiations succeed. Set `ssl_min_protocol_version = 'TLSv1.2'` in `postgresql.conf`.

## 4.x — Access Control

### CIS PG 4.x — Postgres superuser must not own application databases

The built-in `postgres` superuser role should be reserved for administrative tasks only. If application databases are owned by this role, a compromised application gains full superuser privileges across the entire cluster. Create dedicated roles for application database ownership and transfer ownership using `ALTER DATABASE ... OWNER TO`.

### CIS PG 4.x — No additional superuser roles besides postgres

Every superuser role bypasses all permission checks and can read, modify, or delete any data in the cluster. Additional superuser accounts increase the blast radius of credential compromise. Audit existing roles with `SELECT rolname FROM pg_roles WHERE rolsuper = true` and revoke superuser from any role that does not strictly require it.

## 5.x — Performance and Stability

### CIS PG 5.x — Slow query logging must be enabled

The `log_min_duration_statement` parameter logs any statement that exceeds the specified duration in milliseconds. Without this, poorly performing queries go undetected until they cause visible service degradation. Set `log_min_duration_statement` to a positive value (e.g., `1000` for one second) to capture slow queries for analysis.

### CIS PG 5.x — Statement timeout must be configured

The `statement_timeout` parameter sets a maximum execution time for any single statement, after which it is automatically cancelled. Without a timeout, runaway queries can hold locks indefinitely and exhaust server resources. Set `statement_timeout` to a non-zero value (e.g., `'30s'`) appropriate to the workload to prevent unbounded query execution.
