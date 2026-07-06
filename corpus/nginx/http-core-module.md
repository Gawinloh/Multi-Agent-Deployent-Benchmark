---
source: "nginx Documentation: HTTP Core Module"
url: https://nginx.org/en/docs/http/ngx_http_core_module.html
service: "nginx"
section: http
license: "BSD-2-Clause"
retrieved: "2026-07-06"
---

# nginx HTTP Core Module Reference

The HTTP core module provides the foundational directives for configuring HTTP server behaviour, including connection management, request handling, and response tuning.

## File Transfer Optimisation

### sendfile

Enables kernel-level file transfer, bypassing the user-space buffer copy. This significantly improves performance for serving static files.

```nginx
sendfile on;
```

### tcp_nopush

When combined with `sendfile`, this causes nginx to send HTTP response headers and the beginning of a file in a single TCP packet, reducing the number of packets transmitted.

```nginx
tcp_nopush on;
```

### tcp_nodelay

Disables Nagle's algorithm, which buffers small packets. Useful for interactive or real-time applications. Enabled by default on keepalive connections.

```nginx
tcp_nodelay on;
```

Using `sendfile on`, `tcp_nopush on`, and `tcp_nodelay on` together is a standard optimisation combination. They are not contradictory because `tcp_nopush` applies when sending file data and `tcp_nodelay` applies to the final packet and keepalive connections.

## Connection Keep-Alive

### keepalive_timeout

Controls how long an idle keepalive connection remains open. The default is 65 seconds. Lower values free connections sooner under high concurrency; higher values benefit clients making multiple sequential requests.

```nginx
keepalive_timeout 65;
```

### keepalive_requests

Sets the maximum number of requests served over a single keepalive connection before it is closed. The default is 1000. Increasing this value benefits high-throughput applications with persistent clients.

```nginx
keepalive_requests 1000;
```

## Request Body Limits

### client_max_body_size

Sets the maximum allowed size of the client request body. The default is 1 megabyte. If the request exceeds this limit, nginx returns a 413 (Request Entity Too Large) error. Adjust this for applications that accept file uploads.

```nginx
client_max_body_size 50m;
```

For API endpoints that accept large payloads or file upload services, set this to match the maximum expected upload size. Setting it to `0` disables the check entirely, which is generally not recommended.

## Hash Table Configuration

### types_hash_max_size

Controls the size of the hash table used for MIME type lookups. Increase this if nginx logs warnings about hash table size during startup.

```nginx
types_hash_max_size 2048;
```

## Security-Related Directives

### server_tokens

Controls whether nginx includes its version number in error pages and the `Server` response header. Disabling this reduces information exposure to potential attackers.

```nginx
server_tokens off;
```

### autoindex

Controls automatic directory listing when no index file is found. Should be disabled in production to prevent unintended exposure of directory contents.

```nginx
autoindex off;
```

### default_type

Sets the default MIME type for responses when the type cannot be determined from the file extension. The standard default is `application/octet-stream`, which causes browsers to download unknown file types rather than attempting to render them.

```nginx
default_type application/octet-stream;
```

## Recommended HTTP Block Configuration

A production-ready HTTP block combining these directives:

```nginx
http {
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    keepalive_requests 1000;
    types_hash_max_size 2048;
    server_tokens off;
    autoindex off;
    default_type application/octet-stream;
    client_max_body_size 10m;

    include /etc/nginx/mime.types;
}
```

## Provisioning Notes

- The `client_max_body_size` directive is context-dependent and should be set per-location or per-server block when different endpoints have different upload requirements.
- Always set `server_tokens off` and `autoindex off` as security baselines.
- The combination of `sendfile`, `tcp_nopush`, and `tcp_nodelay` is safe for virtually all deployment scenarios.
