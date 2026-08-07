---
source: "RabbitMQ Documentation: TLS Support"
url: https://www.rabbitmq.com/docs/ssl
service: "rabbitmq"
section: ssl
license: "Apache-2.0 (rabbitmq-website)"
retrieved: "2026-08-07"
---

# RabbitMQ TLS Support

RabbitMQ can serve AMQP over TLS on a dedicated listener, conventionally port 5671, alongside or instead of the plaintext listener on 5672.

## Enabling the listener

Three settings are the minimum:

```
listeners.ssl.default = 5671
ssl_options.cacertfile = /path/to/ca_certificate.pem
ssl_options.certfile   = /path/to/server_certificate.pem
ssl_options.keyfile    = /path/to/server_key.pem
```

A CA certificate file is required even when peer verification is disabled, because the Erlang TLS implementation uses it to build the chain it presents. When the server certificate is self-signed, that certificate is its own issuer and can serve as the CA file.

The key file must be readable by the user the broker runs as (`rabbitmq`), and no more widely than necessary. Unlike PostgreSQL, RabbitMQ does not refuse to start on a group-readable key, so a permissions mistake here fails silently rather than loudly.

Removing `listeners.tcp.default` while an SSL listener is configured disables plaintext AMQP entirely. Leaving both means clients may still connect unencrypted, so the presence of a TLS listener alone does not establish that traffic is encrypted.

## Peer verification

`ssl_options.verify` takes `verify_none` or `verify_peer`.

- `verify_none` — any client is accepted. The connection is encrypted, but the broker learns nothing about who is on the other end. Encryption without authentication.
- `verify_peer` — the client must present a certificate that chains to the configured CA. Combined with `ssl_options.fail_if_no_peer_cert = true`, a client with no certificate is rejected outright rather than falling back.

`verify_peer` on its own is weaker than it looks: without `fail_if_no_peer_cert`, a client that simply presents nothing is still allowed to connect. The two settings are meant to be used together.

With the `EXTERNAL` authentication mechanism the client certificate can also *identify* the user, replacing username and password entirely.

## Protocol versions and ciphers

`ssl_options.versions` restricts the accepted TLS versions; current guidance is to offer TLS 1.3 and 1.2 and to refuse 1.0 and 1.1, which are deprecated and have known weaknesses. `ssl_options.ciphers` restricts the cipher suites. RabbitMQ's defaults follow the underlying Erlang/OTP release, so they shift with the runtime version rather than staying fixed.

## Verifying the result

`rabbitmq-diagnostics listeners` prints every bound listener with its protocol. A working TLS listener appears as `amqp/ssl`. This is a stronger check than reading the configuration, because a listener that failed to bind — an unreadable key file, a malformed certificate — leaves the configuration looking correct while the port is simply absent.
