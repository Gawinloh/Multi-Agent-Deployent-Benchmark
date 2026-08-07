---
source: "RabbitMQ Documentation: Production Checklist and Access Control"
url: https://www.rabbitmq.com/docs/production-checklist
service: "rabbitmq"
section: security
license: "Apache-2.0 (rabbitmq-website)"
retrieved: "2026-08-07"
---

# RabbitMQ Users, Credentials and Access Control

RabbitMQ ships with a single user, `guest`, whose password is also `guest`. This account is the most commonly reported RabbitMQ finding in security reviews, and removing or replacing it is the first item on the production checklist.

## The guest account

By default `guest` can only connect from localhost. That restriction comes from the `loopback_users` setting, which ships as `loopback_users.guest = true`. It is a safety net, not a security control: any deployment that clears `loopback_users` — a common change when a client cannot connect — immediately exposes a well-known administrator credential to the whole network.

The production checklist gives two acceptable outcomes:

- **Never create the account.** `default_user` and `default_pass` name the single user RabbitMQ seeds when a node boots with an empty database. Setting `default_user` to anything other than `guest` means the `guest` account is never created at all. This is the cleanest option for a container deployment, where every node starts from a fresh data directory.
- **Delete it after the fact.** `rabbitmqctl delete_user guest` on an existing node. This is the option for a broker that has already been running.

`rabbitmqctl list_users` is the objective test for either. A node whose user list contains no `guest` entry has satisfied the control regardless of which route was taken.

## default_user and default_pass

These settings apply **only on a node's first boot**, when the internal database is empty. On a node with existing state they are ignored entirely, which surprises operators who change them and see no effect. The password should be long and randomly generated; it is a service credential and is never typed by a human.

## Users, tags and permissions

RabbitMQ authorisation has three layers:

- **Virtual hosts** — logical groupings of queues, exchanges and bindings. A user must be granted access to a vhost before it can do anything inside it.
- **User tags** — `administrator`, `monitoring`, `policymaker`, `management`, or none. Tags control access to the management interface and to broker-wide operations. The seeded `default_user` is created with the `administrator` tag.
- **Permissions** — per-vhost regular-expression triples controlling configure, write and read operations on named resources.

The checklist recommends that an application connect as a user with no administrator tag, scoped to a dedicated vhost, with permissions narrowed to the resources it actually uses. An application that only publishes to one exchange does not need permission to delete queues.

## Authentication mechanisms

The default mechanism is `PLAIN` over a TLS connection, which sends credentials in the clear inside the encrypted channel. `AMQPLAIN` and `EXTERNAL` (certificate-based, where the client certificate identifies the user) are also available. `EXTERNAL` removes shared passwords entirely but requires a certificate-issuing process for every client.

## Verifying the result

Two checks establish whether authentication is actually enforced, rather than merely configured:

- `rabbitmqctl authenticate_user guest guest` must fail.
- `rabbitmqctl list_users` must not list an account the deployment did not intend to create.
