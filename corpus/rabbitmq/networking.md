---
source: "RabbitMQ Documentation: Networking"
url: https://www.rabbitmq.com/docs/networking
service: "rabbitmq"
section: networking
license: "Apache-2.0 (rabbitmq-website)"
retrieved: "2026-08-07"
---

# RabbitMQ Networking and Listeners

A RabbitMQ node opens several listeners, each on its own port. Knowing which is which matters, because they carry very different levels of privilege.

| Port | Listener | Purpose |
|---|---|---|
| 5672 | `listeners.tcp` | AMQP 0-9-1 and AMQP 1.0, plaintext |
| 5671 | `listeners.ssl` | the same protocols over TLS |
| 15672 | management plugin | HTTP API and web UI |
| 15692 | prometheus plugin | metrics scraping |
| 25672 | Erlang distribution | inter-node clustering and CLI tools |

## Configuring the AMQP listener

`listeners.tcp.default = 5672` binds every interface. The form `listeners.tcp.default = 192.168.1.10:5672` binds one address, and several `listeners.tcp.<name>` entries may coexist to bind a specific set. The production checklist advises binding to an internal interface rather than a wildcard wherever the deployment topology allows it.

Inside a container network this needs care. Binding to `127.0.0.1` makes the broker unreachable from *other containers*, since each container has its own loopback interface — the effect is a broker that appears healthy to its own health check and refuses every client. `0.0.0.0` inside a container is scoped by the container network and by which ports the runtime publishes, which is a materially different exposure from `0.0.0.0` on a host.

## Erlang distribution

Port 25672 carries inter-node traffic and CLI tool commands. Anyone who can reach it *and* holds the shared Erlang cookie has full control of the node — it is the most privileged listener on the broker. It should never be reachable from outside the cluster's own network. `distribution.listener.interface` and `distribution.listener.port_range` control it.

The Erlang cookie is a shared secret stored at `$HOME/.erlang.cookie`, readable only by its owner. CLI tools read it, and *create it if it is missing*. Running a CLI tool as a different user from the broker can therefore leave a cookie the broker itself cannot read, and the node fails to start with `eacces`.

## Heartbeats

`heartbeat` (seconds, default 60) sets the interval at which peers exchange heartbeat frames. It is the mechanism that detects a peer whose TCP connection has died without a FIN — a hard-powered-off client, or a connection dropped by an intermediary. Setting it to 0 disables detection, and dead connections then accumulate, holding queues, file descriptors and memory until the broker is restarted. Values below about 5 seconds risk false positives on a loaded node.

Note that many network devices and cloud load balancers silently close idle TCP connections after a few minutes, so a heartbeat shorter than that idle timeout is often what keeps a connection alive at all.

## Connection limits

`max_connections` caps the number of simultaneous client connections; `channel_max` caps channels per connection. Both default to effectively unlimited. Each connection costs a file descriptor and a non-trivial amount of memory, so an unbounded limit means one misbehaving client — the classic case being an application that opens a connection per message instead of reusing one — can exhaust the node. The setting was called `connection_max` in older releases and RabbitMQ 4 logs a rename warning if the old name is used.
