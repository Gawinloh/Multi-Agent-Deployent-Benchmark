---
source: "pgAudit Documentation"
url: https://github.com/pgaudit/pgaudit/blob/master/README.md
service: "postgres"
section: auditing
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# pgAudit: PostgreSQL Audit Extension

pgAudit is a PostgreSQL extension that provides detailed session and object audit logging. It extends PostgreSQL's built-in logging to produce audit trails that satisfy compliance requirements such as CIS benchmarks, SOC 2, and PCI-DSS.

## Why pgAudit

PostgreSQL's native `log_statement` parameter can log SQL statements, but it provides limited granularity. Setting `log_statement = all` produces excessive log volume with no filtering capability, while `log_statement = ddl` or `log_statement = mod` may miss reads that are relevant for audit purposes. pgAudit provides class-based filtering, allowing administrators to log specific categories of operations (e.g., only DDL and role changes) without logging every SELECT query.

pgAudit also provides object-level audit logging, which logs statements that affect specific tables or columns. This is configured through the PostgreSQL role system rather than global settings, allowing fine-grained control over what is audited.

## Installation

pgAudit must be loaded as a shared library and then created as an extension in each database that requires audit logging.

1. Add pgAudit to the shared preload libraries in postgresql.conf:

```
shared_preload_libraries = 'pgaudit'
```

This requires a server restart.

2. Create the extension in the target database:

```sql
CREATE EXTENSION pgaudit;
```

## Session Audit Logging

Session audit logging captures statements based on their class. Configure the classes to audit using the `pgaudit.log` parameter.

### pgaudit.log

A comma-separated list of statement classes to log. Available classes:

- **READ**: SELECT and COPY TO statements.
- **WRITE**: INSERT, UPDATE, DELETE, TRUNCATE, and COPY FROM statements.
- **FUNCTION**: Function calls and DO blocks.
- **ROLE**: Statements related to roles and privileges (GRANT, REVOKE, CREATE ROLE, ALTER ROLE, DROP ROLE).
- **DDL**: All DDL statements not covered by ROLE (CREATE TABLE, ALTER TABLE, DROP TABLE, etc.).
- **MISC**: Miscellaneous commands (DISCARD, FETCH, CHECKPOINT, VACUUM, SET).
- **MISC_SET**: SET commands specifically (a subset of MISC).
- **ALL**: Includes all of the above classes.

Example configuration:

```
pgaudit.log = 'ddl, role, write'
```

This logs all schema changes, privilege modifications, and data modifications without capturing every read query.

### pgaudit.log_catalog

Controls whether statements involving pg_catalog tables are logged. The default is `on`. Setting this to `off` reduces log noise from internal system catalog queries that many tools and drivers issue automatically.

### pgaudit.log_relation

When set to `on`, a separate log entry is created for each relation (table, view) referenced in a statement. The default is `off`. Enabling this is useful when you need to know exactly which tables a query touched, at the cost of increased log volume.

### pgaudit.log_statement_once

When set to `on`, the statement text is included only in the first log entry for a multi-relation statement (rather than being repeated in each entry). The default is `off`. Enabling this reduces log size when pgaudit.log_relation is also enabled.

### pgaudit.log_parameter

When set to `on`, parameters passed with the statement are included in the audit log. The default is `off`. Enabling this captures the actual values used in parameterized queries, which may be necessary for complete audit trails but may also expose sensitive data.

## Object Audit Logging

Object audit logging uses the PostgreSQL role system. An auditor role is created and granted permissions on specific tables. pgAudit then logs statements that would require those permissions.

```sql
CREATE ROLE auditor NOLOGIN;
ALTER SYSTEM SET pgaudit.role = 'auditor';
GRANT SELECT ON sensitive_table TO auditor;
```

With this configuration, any SELECT on sensitive_table is logged regardless of the pgaudit.log setting.

## CIS Compliance

The CIS PostgreSQL Benchmark recommends enabling pgAudit to satisfy audit logging requirements. Specific CIS controls addressed include logging of DDL changes, privilege escalation (GRANT/REVOKE), and access to sensitive data. pgAudit's class-based filtering allows organizations to meet these requirements without the performance and storage overhead of logging all statements.
