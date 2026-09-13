# MCP sidecars

Small MCP streamable-HTTP servers that give agents real capabilities via the
platform's generic remote MCP client. Attach them to a tenant from the admin
console (Tenants → MCP) or declare them in a workflow pack's `capabilities`.

| sidecar | port | tools | notes |
|---|---|---|---|
| godot-mcp | 8090 | `run_gdscript`, `godot_version` | live headless-engine evaluation; builds/tests belong to delegation CI |
| comfy-mcp | 8091 | `generate_image` | bridges to a ComfyUI instance (`COMFY_URL`, `COMFY_CHECKPOINT`); serves stored assets at `/assets/{id}` |

Build:

    podman build -t godot-mcp:dev -f Dockerfile.godot .
    podman build -t comfy-mcp:dev -f Dockerfile.comfy .

Run on the host (reachable from a k3s cluster via a host-endpoint Service, the
same pattern as `deploy/k8s/base/ollama-host.yaml`):

    podman run -d --name godot-mcp --restart=always -p 8090:8090 localhost/godot-mcp:dev
    podman run -d --name comfy-mcp --restart=always --network host \
      -e COMFY_URL=http://127.0.0.1:8188 -e PUBLIC_URL=http://comfy-mcp:8091 \
      -v comfy-assets:/assets comfy-mcp:dev

The MCP endpoint is `POST /mcp`; `GET /healthz` is the probe.
