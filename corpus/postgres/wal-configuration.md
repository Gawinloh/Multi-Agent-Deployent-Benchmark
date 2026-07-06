---
source: "PostgreSQL 16 Documentation: Write Ahead Log"
url: https://www.postgresql.org/docs/16/runtime-config-wal.html
service: "postgres"
section: wal
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# Write-Ahead Log (WAL) Configuration

The write-ahead log ensures data integrity by writing changes to a log before they are applied to data files. WAL configuration affects durability, replication, and performance.

## wal_level

Determines how much information is written to the WAL. Possible values are `minimal`, `replica` (the default), and `logical`.

- `minimal` writes only the information needed for crash recovery. It does not support streaming replication or point-in-time recovery.
- `replica` adds enough information to support WAL archiving and streaming replication, including read replicas. This is the default and sufficient for most deployments.
- `logical` adds information needed for logical decoding, which supports logical replication and change data capture (CDC) use cases. This level generates the most WAL data.

Changing wal_level requires a server restart.

## max_wal_size

Specifies the maximum size that WAL files can grow to between automatic checkpoints. The default is 1 GB. When WAL data reaches this threshold, a checkpoint is triggered. Increasing this value reduces checkpoint frequency, which can improve write performance at the cost of longer recovery times after a crash. For write-heavy workloads, values of 2-8 GB are common.

This is a soft limit -- WAL can temporarily exceed this size under heavy load.

## min_wal_size

Sets the minimum size of WAL files retained. The default is 80 MB. WAL files below this threshold are recycled rather than deleted, avoiding the overhead of creating new segment files. This improves performance during write bursts.

## checkpoint_completion_target

Controls the fraction of the checkpoint interval over which checkpoint writes are spread. The default is 0.9, meaning checkpoint I/O is distributed over 90% of the time between checkpoints. This smooths I/O load and prevents performance spikes that occur when a large volume of dirty buffers is flushed at once.

A value of 0.9 is the recommended setting for most workloads. Lower values cause more aggressive flushing, concentrating I/O in shorter bursts.

## checkpoint_timeout

Specifies the maximum time between automatic checkpoints. The default is 5 minutes. Increasing this value (e.g., to 15-30 minutes) reduces checkpoint frequency and the associated I/O overhead, but increases crash recovery time. Combined with a larger max_wal_size, longer checkpoint intervals benefit write-intensive workloads.

## wal_compression

Enables compression of full-page images written to WAL. Supported algorithms include `pglz`, `lz4`, and `zstd`. The default is `off`. Enabling compression reduces WAL volume and network bandwidth for replication, at the cost of additional CPU usage. LZ4 and ZSTD typically offer a good compression-to-CPU tradeoff.

This is particularly beneficial for workloads with large pages and heavy write activity, as it reduces both disk I/O and replication lag.

## archive_mode

Controls whether WAL archiving is enabled. Possible values are `off`, `on`, and `always`. When set to `on`, completed WAL segments are sent to the archive via the command specified in archive_command (or the archive library specified in archive_library). This supports point-in-time recovery (PITR) and backup strategies.

The `always` setting ensures WAL is archived even on standby servers, which is useful for cascading replication or maintaining archives from replicas.

Changing archive_mode requires a server restart.

## archive_command

Specifies the shell command executed to archive a completed WAL segment. The command must return a zero exit status only when the file has been safely stored. The placeholder `%p` is replaced with the path to the WAL file and `%f` with the filename. Example: `cp %p /archive/%f`.

## wal_buffers

Sets the amount of shared memory used for WAL data not yet written to disk. The default of -1 selects a size equal to 1/32 of shared_buffers, capped at 16 MB. For most systems, the auto-tuned default is adequate.
