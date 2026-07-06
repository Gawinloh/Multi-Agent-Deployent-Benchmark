---
source: "PostgreSQL 16 Documentation: Secure TCP/IP Connections with SSL"
url: https://www.postgresql.org/docs/16/ssl-tcp.html
service: "postgres"
section: security
license: "PostgreSQL License"
retrieved: "2026-07-06"
---

# SSL/TLS Configuration

PostgreSQL supports SSL/TLS encryption for TCP/IP connections, protecting data in transit from eavesdropping and tampering. SSL must be enabled at compile time (it is included in most distribution packages) and configured in postgresql.conf.

## Enabling SSL

Set `ssl = on` in postgresql.conf. This requires that a valid server certificate and private key are available. The server will then accept both SSL and non-SSL connections on the same port (5432 by default). To enforce SSL for specific clients, use `hostssl` entries in pg_hba.conf.

## Certificate and Key Files

- **ssl_cert_file**: Path to the server's SSL certificate. The default is `server.crt` in the data directory. The file must contain the server certificate and may optionally include intermediate CA certificates in the chain.
- **ssl_key_file**: Path to the server's private key. The default is `server.key` in the data directory. The file must be owned by the PostgreSQL OS user and have permissions no more permissive than 0600. The server will refuse to start if the key file is world-readable.
- **ssl_ca_file**: Path to the trusted certificate authority (CA) file. When set, the server requests a client certificate and verifies it against this CA. This enables mutual TLS (mTLS) authentication. If not set, client certificates are not requested.
- **ssl_crl_file**: Path to a certificate revocation list (CRL). If provided, client certificates are checked against this list and rejected if they appear as revoked.

## Protocol and Cipher Settings

- **ssl_min_protocol_version**: Sets the minimum SSL/TLS protocol version accepted. The default is `TLSv1.2`. Acceptable values include `TLSv1`, `TLSv1.1`, `TLSv1.2`, and `TLSv1.3`. Setting this to `TLSv1.2` or higher is recommended, as older protocol versions have known vulnerabilities.
- **ssl_max_protocol_version**: Sets the maximum SSL/TLS protocol version. Leaving this unset (empty string) allows all versions up to the highest supported by the OpenSSL library.
- **ssl_ciphers**: Specifies the list of allowed SSL cipher suites. The default is `HIGH:MEDIUM:+3DES:!aNULL`. This string uses OpenSSL cipher list syntax. For stricter security, you can narrow this to specific cipher suites.
- **ssl_prefer_server_ciphers**: When set to `on` (the default), the server's cipher preference order is used instead of the client's. This ensures the server selects the strongest mutually supported cipher.
- **ssl_ecdh_curve**: Specifies the curve used for ECDH key exchange. The default is `prime256v1` (also known as P-256 or secp256r1). This is suitable for most deployments.

## Verifying SSL with psql

To connect using SSL and verify the connection:

```
psql "host=dbserver dbname=mydb user=myuser sslmode=verify-full sslrootcert=/path/to/ca.crt"
```

The `sslmode` parameter controls SSL behavior on the client side:
- `disable`: No SSL.
- `allow`: Try non-SSL first, then SSL if the server requires it.
- `prefer`: Try SSL first, fall back to non-SSL (the default).
- `require`: SSL required, but no certificate verification.
- `verify-ca`: SSL required, server certificate must be signed by a trusted CA.
- `verify-full`: SSL required, server certificate must be signed by a trusted CA and the server hostname must match the certificate's CN or SAN.

For production use, `verify-full` is recommended to prevent man-in-the-middle attacks.

## Key File Permissions

The server private key file must have restrictive permissions. On Linux/Unix, set ownership to the PostgreSQL user and permissions to 0600:

```
chown postgres:postgres server.key
chmod 0600 server.key
```

The server will not start if the key file is accessible by other users.
