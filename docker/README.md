# docker/ — the image and the compose stack

One image (`docker/Dockerfile`), entrypoint selects the process (`api` / `worker` /
`migrate`). Works with Docker or Podman.

**Install with the installer, not raw compose:**

```bash
./install.sh compose        # from the repo root
```

The compose file requires secrets from `.env` (JWT secret, encryption key, database
passwords) and the installer generates them on first run — a bare
`compose up` without that fails. The installer also fetches the workflow packs, wires
the container-engine socket for exec environments, and waits for health. `docs/install.md` is the full guide; `docs/self-host.md` keeps the
manual walkthrough for operators who want to assemble it themselves.

What the stack runs: `postgres` (pgvector), `redis`, `migrate` (one-shot, exits 0),
`api` (:8000, `PYRRHULA_API_PORT` to override), `worker`, `web` (nginx serving the
built UI on :5173, proxying `/api/*`), and
`searxng` (web search for agents that enable it).

Tear down:

```bash
podman compose -f docker/compose.selfhost.yml down -v   # -v also drops the data volumes
```

Running one entrypoint directly, without compose:

```bash
podman build -t pyrrhula:dev -f docker/Dockerfile .
podman run --rm -p 8000:8000 pyrrhula:dev api
podman run --rm pyrrhula:dev worker
podman run --rm -e PYRRHULA_DATABASE_URL=... pyrrhula:dev migrate
```

`docker/helm/` is reserved for a future Helm chart and holds only a placeholder README.
