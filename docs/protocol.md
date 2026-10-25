# Local fixture HTTP protocol

This is a project-specific protocol, not MCP or JSON-RPC. It is for a trusted local development machine. State is held only in the server process. Nothing is proxied to a real tool.

Start `tool-mockbox serve examples/healthy.json --port 8769`. The printed address uses `127.0.0.1`; `--port 0` selects an available port. All requests need exactly one `Host` identifying `127.0.0.1:PORT` or `localhost:PORT`. An absent `Origin` is permitted for native clients. A supplied `Origin` must be the exact `http://127.0.0.1:PORT` or `http://localhost:PORT`; duplicate/foreign/null origins are rejected. No CORS headers are issued.

## Endpoints

| Request | Response |
| --- | --- |
| `GET /catalog` | `schema_version`, tool names/descriptions, and total/consumed/remaining step counts |
| `GET /events` | `schema_version`, `wait_mode`, summary counts, and recorded events |
| `POST /call` | Consume the next matching step for one tool |

Query strings and alternate route spellings are not accepted. `OPTIONS` returns 403. Other unsupported methods return a JSON 501 response. Connections close after every response.

### Call example

```http
POST /call HTTP/1.0
Host: 127.0.0.1:8769
Content-Type: application/json
Content-Length: 43

{"tool":"lookup","arguments":{"id":"demo"}}
```

Use an HTTP library to calculate `Content-Length` from the actual UTF-8 bytes. The example JSON body above is 43 bytes, excluding a trailing newline. Content type parameters are allowed. There must be one decimal nonnegative Content-Length, no Transfer-Encoding, and a body of at most 65,536 bytes. The socket has a 5-second inactivity timeout; this is not a guaranteed total wall-clock deadline. Duplicate JSON keys, unsupported numbers, invalid UTF-8, and unknown request fields are rejected before a step is consumed.

Success, HTTP 200:

```json
{"ok":true,"result":{"title":"Release checklist"}}
```

Scripted tool-level failure, also HTTP 200:

```json
{"ok":false,"call_id":"call-1","error":{"code":"temporary","message":"Synthetic temporary failure"}}
```

The second response is valid fixture protocol traffic. It does not claim that an actual HTTP service returned an error. Scripted failures consume their matching step.

Unknown tool, mismatched arguments, exhausted sequence, or log limit, HTTP 409:

```json
{"ok":false,"call_id":"call-2","error":{"code":"arguments_mismatch","message":"Arguments do not match the next scripted step"}}
```

These do not consume the next step. The first three are recorded as rejected calls. A log-limit rejection cannot be recorded and has `call_id: null`.

## HTTP validation errors

| Status | Examples |
| --- | --- |
| 400 | Bad/duplicate length, Transfer-Encoding, incomplete body, invalid JSON or request schema |
| 403 | Rejected Host/Origin, OPTIONS |
| 404 | Unknown route |
| 408 | Body read timed out |
| 411 | Missing Content-Length |
| 413 | Body too large, excessively long length token |
| 415 | Missing, duplicate, or non-JSON Content-Type |

Errors use `{"ok":false,"error":{"code":"...","message":"..."}}` with no event ID. These validation failures create no event and do not consume a step. Responses include `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. Arguments and results in `/events` are not redacted; no authentication is provided.
