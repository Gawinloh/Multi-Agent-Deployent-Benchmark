---
source: "RabbitMQ Documentation: Management Plugin"
url: https://www.rabbitmq.com/docs/management
service: "rabbitmq"
section: management
license: "Apache-2.0 (rabbitmq-website)"
retrieved: "2026-08-07"
---

# RabbitMQ Management Plugin

The management plugin provides a web UI and an HTTP API for administering and monitoring a broker. It is not enabled by default in the plain server distribution; the `rabbitmq:*-management` container images enable it at build time, so a deployment using those images always has it loaded.

## What it exposes

The HTTP API is a full administrative surface. Given credentials with the `administrator` tag it can create and delete users, vhosts, queues and exchanges, publish and consume messages, and read broker configuration. It is not a read-only dashboard, and it should be treated with the same care as an SSH port.

Access is governed by user tags:

- `management` — sees only its own vhosts' objects
- `policymaker` — plus policy and parameter management
- `monitoring` — plus node-wide metrics across all vhosts
- `administrator` — everything, including user management

## Listener configuration

```
management.tcp.port = 15672
management.tcp.ip   = 127.0.0.1
```

`management.tcp.ip` is the important one. Left unset, the listener binds all interfaces. Binding it to a loopback or internal address keeps the administrative surface off the general network while leaving it reachable through an SSH tunnel or from a sidecar. The production checklist treats an internet-reachable management interface as a finding.

`management.ssl.*` serves the same interface over HTTPS with its own certificate settings, which matters because HTTP basic credentials are otherwise sent in the clear on every request.

## Enabling and disabling

`rabbitmq-plugins enable rabbitmq_management` and the matching `disable` load and unload it at runtime; `rabbitmq-plugins is_enabled rabbitmq_management` reports the current state. Configuration settings under `management.*` only move the listener — they cannot unload the plugin. A broker that has no need for the UI is best deployed from an image that never enabled it, since a plugin that is not loaded has no attack surface at all.

## Health checks without it

The management API's `/api/healthchecks/node` endpoint is a common health check, but it requires credentials and the plugin. The CLI equivalents need neither and are the better choice for a container health check:

- `rabbitmq-diagnostics -q ping` — the node responds
- `rabbitmq-diagnostics -q check_running` — the application is fully started
- `rabbitmq-diagnostics -q check_port_connectivity` — listeners accept connections

These are Erlang CLI tools, so they must run as the same user as the broker; see the note on the Erlang cookie in `networking.md`.
