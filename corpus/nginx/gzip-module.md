---
source: "nginx Documentation: ngx_http_gzip_module"
url: https://nginx.org/en/docs/http/ngx_http_gzip_module.html
service: "nginx"
section: compression
license: "BSD-2-Clause"
retrieved: "2026-07-06"
---

# nginx Gzip Compression Module Reference

The `ngx_http_gzip_module` enables on-the-fly gzip compression of responses, reducing the amount of data transferred to clients. Compression is particularly effective for text-based content types.

## Enabling Compression

### gzip

The master switch for gzip compression. When set to `on`, nginx compresses responses before sending them to clients that advertise gzip support via the `Accept-Encoding` request header.

```nginx
gzip on;
```

## Core Configuration Directives

### gzip_vary

Adds the `Vary: Accept-Encoding` header to compressed responses. This is important for caching proxies and CDNs, informing them that the response varies based on whether the client supports compression. Without this, a proxy might serve a compressed response to a client that cannot decompress it.

```nginx
gzip_vary on;
```

### gzip_proxied

Determines whether responses from proxied requests should be compressed. The value `any` enables compression for all proxied requests regardless of the request or response headers. Other options allow fine-grained control based on cache-control headers.

```nginx
gzip_proxied any;
```

### gzip_comp_level

Sets the compression level from 1 (fastest, least compression) to 9 (slowest, most compression). Levels 4 through 6 provide a good balance between CPU usage and compression ratio. Beyond level 6, the marginal improvement in compression is small relative to the additional CPU cost.

```nginx
gzip_comp_level 5;
```

### gzip_min_length

Sets the minimum response size (based on the `Content-Length` header) required to trigger compression. Very small responses gain little from compression and the overhead of the gzip headers can even make them larger. A value of 256 bytes is a practical threshold.

```nginx
gzip_min_length 256;
```

### gzip_types

Specifies which MIME types are eligible for compression. The type `text/html` is always compressed when gzip is enabled and does not need to be listed. Other common compressible types should be included explicitly.

```nginx
gzip_types
    text/plain
    text/css
    text/xml
    text/javascript
    application/json
    application/javascript
    application/x-javascript
    application/xml
    application/xml+rss
    application/atom+xml
    image/svg+xml;
```

Binary formats such as images (JPEG, PNG), videos, and already-compressed archives should not be listed, as attempting to compress them wastes CPU cycles with negligible size reduction.

### gzip_buffers

Configures the number and size of buffers used for compressing responses. The default is typically adequate, but it can be tuned for servers handling very large responses.

```nginx
gzip_buffers 16 8k;
```

### gzip_disable

Disables compression for requests matching the specified user-agent pattern. The common use case is excluding Internet Explorer 6, which has known issues with gzip handling.

```nginx
gzip_disable "msie6";
```

The special value `msie6` is a built-in shorthand that matches the IE6 user-agent string.

## Recommended Gzip Configuration

A production-ready gzip configuration block:

```nginx
http {
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 5;
    gzip_min_length 256;
    gzip_disable "msie6";
    gzip_buffers 16 8k;
    gzip_types
        text/plain
        text/css
        text/xml
        text/javascript
        application/json
        application/javascript
        application/x-javascript
        application/xml
        application/xml+rss
        application/atom+xml
        image/svg+xml;
}
```

## Provisioning Notes

- Gzip compression is CPU-intensive at higher levels. For high-traffic servers, prefer `gzip_comp_level` between 4 and 6.
- Always include `gzip_vary on` when a caching layer or CDN sits in front of nginx.
- When pre-compressed static assets are available (via build tools), consider the `ngx_http_gzip_static_module` to serve `.gz` files directly, avoiding runtime compression entirely.
- Ensure `gzip_types` covers all text-based content types served by the application but excludes binary formats.
