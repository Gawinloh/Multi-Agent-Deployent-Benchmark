---
source: "Mozilla SSL Configuration Generator - Intermediate Profile"
url: https://ssl-config.mozilla.org/
section: ssl-best-practice
license: "MPL-2.0"
retrieved: "2026-07-06"
---

# Mozilla SSL Configuration: Intermediate Profile

## Profile Overview

Mozilla defines three SSL/TLS configuration profiles: Modern, Intermediate, and Old. The Intermediate profile is recommended for most public-facing servers. It balances strong security with broad client compatibility, supporting clients back to Firefox 27, Android 4.4.2, Chrome 31, IE 11 on Windows 7, Java 8u31, and Safari 9.

## Supported Protocols

- TLSv1.2 and TLSv1.3
- TLSv1.0 and TLSv1.1 are explicitly disabled (deprecated by RFC 8996)

TLSv1.3 is preferred when both client and server support it, as it provides a faster handshake (1-RTT, or 0-RTT for resumption) and removes legacy cipher suites with known weaknesses.

## Recommended Cipher String

For servers that support both TLSv1.2 and TLSv1.3:

```
TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305
```

Key properties of this cipher string:
- All ciphers provide forward secrecy (ECDHE or DHE key exchange)
- All ciphers use authenticated encryption (GCM or POLY1305)
- No CBC-mode ciphers (eliminates padding oracle risks)
- No RSA key exchange (no forward secrecy without ephemeral keys)
- TLS 1.3 cipher suites are listed first (TLS_AES_*)

## HSTS (HTTP Strict Transport Security)

```
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
```

- **max-age=63072000**: 2 years (730 days). Browsers will refuse plaintext HTTP connections to this domain for this duration after the first visit.
- **includeSubDomains**: Applies HSTS to all subdomains, preventing mixed-content attacks via subdomain takeover.
- **preload**: Signals intent to be included in browser HSTS preload lists (requires separate submission to hstspreload.org).

## OCSP Stapling

Enable OCSP stapling so the server fetches and caches the certificate revocation status from the CA and presents it during the TLS handshake. This avoids the client making a separate OCSP request, which improves handshake latency and user privacy (the CA does not learn which clients visit the site).

Configuration (nginx example):
```
ssl_stapling on;
ssl_stapling_verify on;
resolver 1.1.1.1 8.8.8.8 valid=300s;
resolver_timeout 5s;
```

## Diffie-Hellman Parameters

Generate DH parameters of at least 2048 bits:
```
openssl dhparam -out /etc/ssl/dhparam.pem 2048
```

2048-bit DH provides approximately 112 bits of security, aligned with the strength of a 2048-bit RSA key. The parameter file is used only for DHE cipher suites (ECDHE uses named curves and does not require a dhparam file).

## Session Tickets

Disable TLS session tickets for forward secrecy:
```
ssl_session_tickets off;
```

Session tickets encrypt session state with a server-side key. If this key is compromised, an attacker can decrypt past sessions. Disabling tickets forces session resumption via server-side session caches, which are discarded on restart, preserving forward secrecy.

## Modern Profile (TLS 1.3 Only)

For internal services where client compatibility is controlled, the Modern profile restricts to TLSv1.3 only. This eliminates all TLSv1.2 cipher negotiation complexity and provides the strongest defaults. Suitable for service-to-service communication, internal APIs, and microservice meshes where all endpoints run current software.

## When to Use Each Profile

| Profile      | Use Case                              | Min Client          |
|-------------|---------------------------------------|---------------------|
| Modern       | Internal services, APIs               | Firefox 63, Chrome 70 |
| Intermediate | Public websites, most servers         | Firefox 27, Chrome 31 |
| Old          | Legacy systems (avoid if possible)    | Firefox 1, IE 6     |
