---
source: "CIS nginx Benchmark v2.0 (paraphrased extracts)"
url: https://www.cisecurity.org/benchmark/nginx
service: nginx
license: "CIS Terms of Use (non-commercial research)"
retrieved: "2026-07-06"
---

# CIS nginx — Level 1 Controls (Implemented)

The following controls are paraphrased summaries of CIS Benchmark recommendations relevant to the project's nginx validator. Only controls that the validator checks are included.

## 2.4.x — Request Limits

### CIS nginx 2.4.x — client_max_body_size must be set explicitly

The `client_max_body_size` directive limits the maximum size of a client request body. Without an explicit limit, nginx uses its default of 1 MB, but relying on defaults is fragile and may not match the application's requirements. Set `client_max_body_size` to an appropriate value (e.g., `1m` or `10m`) in the `http`, `server`, or `location` block to prevent excessively large uploads from consuming disk and memory resources.

## 2.5.x — Information Disclosure

### CIS nginx 2.5.1 — Server version tokens must be hidden

By default, nginx includes its version number in the `Server` HTTP response header and on error pages. Exposing the exact version helps attackers identify known vulnerabilities specific to that release. Set `server_tokens off` in the `http` block to suppress version information from responses.

### CIS nginx 2.5.2 — Directory listing (autoindex) must be disabled

When `autoindex` is enabled, nginx generates an HTML listing of directory contents for any request that maps to a directory without an index file. This can expose sensitive files, backup archives, or configuration data to unauthenticated users. Ensure `autoindex off` is set globally and is not overridden in any `server` or `location` block.

## 3.x — Logging

### CIS nginx 3.1 — Access logging must be enabled

Access logs record every client request including the source IP, requested URI, response code, and user-agent. Disabling access logging with `access_log off` removes the primary data source for detecting abuse, debugging errors, and performing forensic analysis. Ensure that every `server` block has `access_log` pointed to a valid log file path, and that no block sets `access_log off`.

## 4.1.x — TLS Configuration

### CIS nginx 4.1.x — Only TLS 1.2 and 1.3 may be enabled

Older protocol versions including SSLv3, TLS 1.0, and TLS 1.1 have known cryptographic weaknesses that allow traffic interception and decryption. Permitting these versions exposes users to downgrade attacks even when stronger protocols are available. Configure `ssl_protocols TLSv1.2 TLSv1.3` to restrict nginx to only the currently secure protocol versions.

### CIS nginx 4.1.x — Server cipher preference must be enforced

When `ssl_prefer_server_ciphers` is off, the client chooses the cipher suite, which may select a weaker algorithm for compatibility. Enabling server-side cipher preference ensures the strongest mutually supported cipher is always used. Set `ssl_prefer_server_ciphers on` alongside a curated `ssl_ciphers` directive that excludes weak algorithms.

### CIS nginx 4.1.x — HTTP must redirect to HTTPS

Serving content over unencrypted HTTP allows network observers to intercept and modify traffic in transit. A permanent redirect (HTTP 301) from port 80 to the HTTPS equivalent ensures all client communication is encrypted. Configure a dedicated `server` block listening on port 80 that returns `301 https://$host$request_uri` for all requests.

## 5.3.x — Security Headers

### CIS nginx 5.3.1 — X-Frame-Options header must be configured

The `X-Frame-Options` header instructs browsers whether the page may be embedded in frames or iframes. Without this header, the site is vulnerable to clickjacking attacks where a malicious page overlays invisible frames to capture user clicks. Add `add_header X-Frame-Options "DENY"` or `"SAMEORIGIN"` to prevent framing by untrusted origins.

### CIS nginx 5.3.2 — X-Content-Type-Options header must be configured

Browsers may attempt to "sniff" the MIME type of a response, interpreting a file differently from what the server declared. This can lead to script execution from files served with non-executable MIME types. Add `add_header X-Content-Type-Options "nosniff"` to instruct browsers to strictly follow the declared Content-Type.

### CIS nginx 5.3.3 — Strict-Transport-Security (HSTS) header must be configured

The HSTS header tells browsers to only connect to the site over HTTPS for a specified period, preventing protocol downgrade attacks and cookie hijacking. Without HSTS, users who type the bare domain or follow an HTTP link remain vulnerable during the initial plaintext request. Add `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains"` to enforce HTTPS-only access for one year.
