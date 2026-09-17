# Script authoring

[Back to README](../README.md)

Only administrators should install scripts. The runner limits resource use but
executes Python with the service account's filesystem, network and inherited
environment access. It refuses execution as root.

## Endpoint and return value

Save this as `scripts/example.py`:

```python
def main():
    return {"status": "ok", "value": 42}
```

Request `/example` with the normal API key and IP permissions. The response is
the returned dictionary itself. `main()` takes no arguments; query strings are
not passed to scripts. Values in the dictionary must be JSON-serializable.

A string result is wrapped as `{"stdout": "...", "trimmed": false}`. Strings
are truncated to 256 characters, with `trimmed: true` when needed. Other return
types, import errors, missing `main()`, serialization failures and exceeded
limits cause HTTP 422 (`Endpoint error`). Details are available in service logs.
Returning a dictionary containing `error` does not by itself change HTTP 200.

Nested names work: `scripts/examples/date.py` is `/examples/date`. Names are
case-sensitive and contain ASCII letters, digits, `_`, `.`, `-` and directory
separators, up to 256 characters excluding the leading slash. `.`/`..` path
components and symlinks escaping the resolved `scripts/` directory are rejected.
Built-in routes take precedence over script paths, so avoid name collisions.
Unknown scripts normally return HTTP 400 through the catch-all route.

## Imports and working directory

Each invocation starts a fresh interpreter in a private temporary directory.
The script's adjacent `lib/` directory is prepended to the Python import path
and linked as `lib/` inside that temporary directory:

```text
scripts/
  example.py          # import util
  lib/
    util.py
  nested/
    report.py         # uses nested/lib, not scripts/lib
    lib/
      helper.py
```

The supplied `/test` script demonstrates importing `scripts/lib/util.py`.
The repository root and script directory are not the invocation's working
directory. Temporary directories are cleaned up after execution.

## Limits

| Resource | Limit |
| --- | --- |
| Parent-enforced elapsed time | 5 seconds, covering interpreter startup, imports, execution and result collection |
| CPU time | 5 seconds per process |
| Address space | 128 MiB per process |
| Open file descriptors | 64 |
| Regular-file size / core dumps | 0 bytes |
| Serialized result channel | 1 MiB, including the result wrapper |
| Captured stdout and stderr | First 64 KiB combined; additional output is drained and discarded |
| Returned string | First 256 characters |

stdout and stderr are merged together, separately from the result channel, so
printing does not corrupt JSON. Captured output is logged; it is not the HTTP
response. The parent kills the process group during cleanup, including on a
timeout. These are process resource controls, not a complete descendant or
host-resource isolation mechanism. Large imports may fail within the memory
limit and regular-file writes are constrained by the zero file-size limit.

## Supplied examples

| Endpoint | Behavior |
| --- | --- |
| `/examples/date` | Local date/time as a string |
| `/examples/str` | Wrapped `Hello World!` string |
| `/examples/iface0` | Interface selected by an IPv4 route lookup toward `8.8.8.8`; not a complete routing-table discovery |
| `/examples/sleep` | Deliberately never finishes; demonstrates timeout/HTTP 422 |
| `/test` | Returns 42 from an adjacent library helper |
