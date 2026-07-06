---
source: "Mozilla SSL Configuration Generator"
url: https://ssl-config.mozilla.org/
service: "nginx"
section: ssl-tuning
license: "MPL-2.0"
retrieved: "2026-07-06"
---

# Mozilla SSL Configuration Generator — nginx Reference

The Mozilla SSL Configuration Generator provides vetted TLS configurations for common web servers. It offers three profiles (Modern, Intermediate, and Old) that balance security strength with client compatibility. For nginx deployments requiring broad compatibility, the Intermediate profile is the standard recommendation.

## Profile Comparison

### Intermediate Profile

Supports TLS 1.2 and TLS 1.3. Compatible with the vast majority of clients, including older Android devices (4.4.2+), Windows 7 with IE 11, and Java 8u31+. This is the appropriate default for public-facing services.

### Modern Profile

Restricts to TLS 1.3 only. Provides the strongest security posture but excludes clients that do not support TLS 1.3, such as older mobile devices, legacy enterprise browsers, and some automated tools. Suitable for internal services or APIs where all clients are known to support TLS 1.3.

### When to Choose Each

Use **Intermediate** when the service is public-facing, must support a range of client versions, or when compatibility requirements are uncertain. Use **Modern** when all clients are known to support TLS 1.3, such as internal microservices, modern API consumers, or environments where the client fleet is controlled.

## Intermediate Profile Configuration

### Protocols and Ciphers

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305;
ssl_prefer_server_ciphers off;
```

Note: In the Intermediate profile, `ssl_prefer_server_ciphers` is set to `off`. This defers to the client's cipher preference, which allows clients that support newer, faster ciphers (such as ChaCha20 on mobile devices) to select them. This differs from some older hardening guides that recommend `on`.

### HSTS

The recommended HSTS `max-age` for the Mozilla Intermediate profile is 63072000 seconds (approximately two years).

```nginx
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
```

### OCSP Stapling

OCSP stapling is recommended to improve TLS handshake performance and client privacy.

```nginx
ssl_stapling on;
ssl_stapling_verify on;
ssl_trusted_certificate /etc/nginx/ssl/ca-chain.crt;
resolver 1.1.1.1 8.8.8.8 valid=300s;
resolver_timeout 5s;
```

A DNS resolver must be configured for nginx to fetch OCSP responses. Using multiple resolvers provides redundancy.

### Diffie-Hellman Parameters

A 2048-bit DH parameter file is required for DHE cipher suites in the Intermediate profile.

```nginx
ssl_dhparam /etc/nginx/ssl/dhparam.pem;
```

Generate with: `openssl dhparam -out /etc/nginx/ssl/dhparam.pem 2048`

### Session Configuration

```nginx
ssl_session_timeout 1d;
ssl_session_cache shared:MozSSL:10m;
ssl_session_tickets off;
```

## Complete Intermediate Profile Example

```nginx
server {
    listen 443 ssl http2;
    server_name example.com;

    ssl_certificate /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;

    ssl_session_timeout 1d;
    ssl_session_cache shared:MozSSL:10m;
    ssl_session_tickets off;

    ssl_dhparam /etc/nginx/ssl/dhparam.pem;

    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_trusted_certificate /etc/nginx/ssl/ca-chain.crt;
    resolver 1.1.1.1 8.8.8.8 valid=300s;
    resolver_timeout 5s;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
}
```

## Provisioning Notes

- The Intermediate profile is the safest default for automated provisioning, as it covers the broadest client base without permitting insecure protocols.
- The `ssl_prefer_server_ciphers off` setting in the Intermediate profile is intentional and differs from common hardening advice; it allows mobile clients to prefer ChaCha20 when beneficial.
- Always configure a DNS resolver when enabling OCSP stapling; without one, nginx cannot fetch OCSP responses and stapling silently fails.
- Regenerate the DH parameter file if it was generated with fewer than 2048 bits.
