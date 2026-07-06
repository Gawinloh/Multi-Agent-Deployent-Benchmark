---
source: "nginx Documentation: ngx_http_ssl_module"
url: https://nginx.org/en/docs/http/ngx_http_ssl_module.html
service: "nginx"
section: ssl
license: "BSD-2-Clause"
retrieved: "2026-07-06"
---

# nginx SSL/TLS Module Reference

The `ngx_http_ssl_module` provides directives for configuring HTTPS on nginx, including certificate management, protocol and cipher selection, session caching, and OCSP stapling.

## Certificate Configuration

### ssl_certificate and ssl_certificate_key

Specify the paths to the server's PEM-formatted certificate chain and private key. The certificate file should include the full chain (server certificate followed by intermediate certificates).

```nginx
ssl_certificate /etc/nginx/ssl/server.crt;
ssl_certificate_key /etc/nginx/ssl/server.key;
```

## Protocol and Cipher Selection

### ssl_protocols

Defines which TLS protocol versions are accepted. Current best practice is to allow only TLS 1.2 and TLS 1.3, as older versions (SSLv3, TLS 1.0, TLS 1.1) have known vulnerabilities and are deprecated.

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
```

### ssl_ciphers

Specifies the permitted cipher suites. The Mozilla intermediate compatibility profile is a widely recommended baseline that balances security with broad client support.

```nginx
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384;
```

### ssl_prefer_server_ciphers

When enabled, the server's cipher preference order takes precedence over the client's. This ensures the strongest mutually-supported cipher is selected.

```nginx
ssl_prefer_server_ciphers on;
```

## Session Management

### ssl_session_cache

Configures a shared session cache that stores TLS session parameters across worker processes. This allows session resumption, reducing the overhead of repeated TLS handshakes.

```nginx
ssl_session_cache shared:SSL:10m;
```

A 10 MB shared cache can hold approximately 40,000 sessions.

### ssl_session_timeout

Sets how long a cached session remains valid. A value of one day balances performance with security.

```nginx
ssl_session_timeout 1d;
```

### ssl_session_tickets

Session tickets provide another mechanism for TLS session resumption, but they can compromise forward secrecy if ticket keys are not rotated frequently. Disabling them is recommended when forward secrecy is a priority.

```nginx
ssl_session_tickets off;
```

## OCSP Stapling

OCSP stapling allows the server to include a cached OCSP response during the TLS handshake, so the client does not need to contact the certificate authority separately to verify certificate revocation status. This improves handshake performance and client privacy.

### ssl_stapling and ssl_stapling_verify

```nginx
ssl_stapling on;
ssl_stapling_verify on;
```

### ssl_trusted_certificate

Provides the CA certificate chain used to verify the OCSP response. This is typically the same chain file used for the certificate itself.

```nginx
ssl_trusted_certificate /etc/nginx/ssl/ca-chain.crt;
```

## Diffie-Hellman Parameters

### ssl_dhparam

Specifies a custom Diffie-Hellman parameter file for DHE cipher suites. The key should be at least 2048 bits. Generate with: `openssl dhparam -out /etc/nginx/ssl/dhparam.pem 2048`.

```nginx
ssl_dhparam /etc/nginx/ssl/dhparam.pem;
```

## Complete SSL Server Block Example

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    ssl_certificate /etc/nginx/ssl/server.crt;
    ssl_certificate_key /etc/nginx/ssl/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_trusted_certificate /etc/nginx/ssl/ca-chain.crt;
    ssl_dhparam /etc/nginx/ssl/dhparam.pem;
}
```

## Provisioning Notes

- Certificate and key file paths must be absolute and readable by the nginx worker process.
- Disabling `ssl_session_tickets` is the safer default when automated key rotation is not in place.
- OCSP stapling requires that the server can reach the CA's OCSP responder; verify outbound connectivity in firewalled environments.
