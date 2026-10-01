# The webhook builder contract (schema 1)

Pyrrhula never builds images itself. An organization's Dockerfile, and every `RUN` step in
it, runs on a builder the operator declares (**Admin ▸ Builders**). If your build system is
not one of the named kinds, put a small receiver in front of it that speaks this contract.
[`webhook_receiver.py`](webhook_receiver.py) is a working reference to start from.

## What you receive

```
POST <submit_url>
Content-Type: application/json
X-Pyrrhula-Timestamp: 1790812800
X-Pyrrhula-Signature: sha256=<hex>
Authorization: Bearer <token>            (only if you set a token)

{
  "schema": 1,
  "build_id": "4f0c…",                   // Pyrrhula's id, for your logs
  "dockerfile_b64": "RlJPTSBkZWJp…",     // the Dockerfile, base64
  "target_ref": "registry.example.com/pyrrhula/t<org>/godot-node:<hash>-<id>",
  "labels": {"pyrrhula.build": "…", "pyrrhula.content-hash": "…"}
}
```

Answer `200` or `202` with `{"external_id": "<id>", "url": "<optional link to the run>"}`.
`external_id` is 1–128 characters of `A-Z a-z 0-9 . _ -`.

Then Pyrrhula polls:

```
GET <status_url>/<external_id>
→ {"state": "queued" | "building" | "succeeded" | "failed" | "cancelled",
   "digest": "sha256:…",       // when succeeded: what you pushed (optional, checked)
   "log_tail": "…",            // optional, last few KB of output
   "error": "…",               // when failed
   "url": "…"}                 // optional
```

and, if you configured a cancel URL, `POST <cancel_url>/<external_id>` when someone presses
Cancel or the build passes the deployment's timeout.

## Verifying the signature

The signature is `HMAC-SHA256(signing_secret, "<timestamp>\n<METHOD>\n<path>\n<body>")`,
hex-encoded and prefixed with `sha256=`. `<path>` is the request path including the query
string; `<body>` is the raw bytes (empty for a `GET`). Refuse a request whose signature
does not match, or whose timestamp is more than a few minutes from your clock — that is
what stops anyone else from building on your machines, or replaying an old request.

```python
import hashlib, hmac, time

def verify(secret, headers, method, path, body, tolerance=300):
    ts = headers.get("x-pyrrhula-timestamp", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance:
        return False
    message = f"{ts}\n{method}\n{path}\n".encode() + body
    expected = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers.get("x-pyrrhula-signature", ""), expected)
```

## Rules a receiver must keep

1. **Never put the Dockerfile in a shell command.** Decode `dockerfile_b64` to a file and
   hand the file to the builder (`docker build -f <file> <empty dir>`). A receiver that
   writes `echo "$DOCKERFILE" | …` turns a Dockerfile into commands on your machine.
2. **Build with an empty context.** Pyrrhula's images are toolchains; the Dockerfile
   validator refuses `ADD` and `COPY` without `--from`, and your receiver should give the
   build nothing to copy anyway.
3. **Push exactly `target_ref`.** It is inside the organization's own namespace in the
   registry the builder was declared with. Pyrrhula asks that registry for the digest and
   refuses the result if it differs from what you report.
4. **Hold the push credential yourself.** Pyrrhula never sends one; it only reads.
5. **Isolate builds from your network** the way you would for any untrusted code: these are
   tenants' `RUN` steps.

## What Pyrrhula does with the result

After `succeeded`, Pyrrhula resolves the digest of `target_ref` from the registry with the
registry's read credential (your reported digest must match), smoke-tests the image on
the organization's own execution engine (git present; a baked harness actually there), and
only then offers it as a build runtime, pinned by digest.
