---
source: "PostgreSQL 16 Documentation: Error Reporting and Logging"
url: https://www.postgresql.org/docs/16/runtime-config-logging.html
service: "postgres"
section: logging
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# Logging Configuration

PostgreSQL provides extensive logging capabilities for diagnostics, auditing, and performance analysis. Logging parameters are configured in postgresql.conf.

## log_destination

Specifies where server log output is sent. Supported values are `stderr` (the default), `csvlog`, `jsonlog`, and `syslog`. Multiple destinations can be specified as a comma-separated list. The `csvlog` and `jsonlog` formats produce structured output suitable for ingestion by log management tools. When using `csvlog` or `jsonlog`, the logging_collector must be enabled.

## logging_collector

Controls whether a background process is launched to capture log output written to stderr and redirect it to log files. The default is `off`, but enabling it (`on`) is recommended for production systems. Without the logging collector, stderr output may be lost depending on how the server was started.

This parameter requires a server restart to change.

## log_directory

Specifies the directory where log files are created when the logging collector is enabled. The default is `log`, which is relative to the data directory. An absolute path can be specified.

## log_filename

Sets the filename pattern for log files. The default is `postgresql-%Y-%m-%d_%H%M%S.log`. The pattern uses strftime-style format codes. Common alternatives include `postgresql-%a.log` (day-of-week rotation, overwriting weekly) or `postgresql-%Y-%m-%d.log` (daily files).

## log_rotation_age and log_rotation_size

Control when log files are rotated. log_rotation_age specifies the maximum age of a log file before rotation (default 24 hours, set to 0 to disable time-based rotation). log_rotation_size specifies the maximum size (default 10 MB, set to 0 to disable size-based rotation).

## log_statement

Controls which SQL statements are logged. Possible values:
- `none` (default): No statements are logged.
- `ddl`: Logs data definition statements (CREATE, ALTER, DROP).
- `mod`: Logs DDL plus data-modifying statements (INSERT, UPDATE, DELETE, TRUNCATE, COPY FROM).
- `all`: Logs every statement. This generates significant volume and is typically used only for debugging or detailed auditing.

## log_connections and log_disconnections

When enabled, `log_connections` logs each successful connection attempt, including the client address, username, and database. `log_disconnections` logs session terminations along with the session duration. Both default to `off`. Enabling both provides a complete session lifecycle audit trail.

## log_min_duration_statement

Logs the execution time and text of any statement that runs for at least the specified duration (in milliseconds). The default is -1 (disabled). Setting this to 0 logs all statements with their durations. Setting it to a positive value (e.g., 1000 for one second) enables slow query logging, which is valuable for identifying performance bottlenecks without the overhead of logging every statement.

This is the recommended approach for slow query detection in production.

## log_line_prefix

Specifies a printf-style format string prepended to each log line. A recommended format for production is:

```
log_line_prefix = '%m [%p] %q%u@%d '
```

This produces output with the timestamp (`%m`), process ID (`%p`), username (`%u`), and database name (`%d`). The `%q` escape suppresses the prefix for non-session processes (background workers), keeping log output clean.

Other useful escapes include `%r` (remote host and port), `%a` (application name), and `%l` (session line number).

## log_checkpoints

When enabled, logs information about each checkpoint including the number of buffers written and the time spent. Useful for monitoring checkpoint performance and tuning checkpoint intervals. Default is `on` in PostgreSQL 16.

## log_lock_waits

When enabled, logs a message if a session waits longer than deadlock_timeout for a lock. Default is `off`. Enabling this helps identify lock contention issues.
