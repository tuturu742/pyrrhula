# Plugin drop directory

Workflow packs you supply by hand, for installs that cannot reach the pinned plugin
repositories in `deploy/plugins.json` (private repo, air-gapped host, or simply offline).
The install never asks for git credentials; this is one of the two ways to add packs
afterwards — the other is **admin console → Plugin repositories → Upload pack**.

Copy each pack here as its own directory — the one containing `plugin.json`:

    docker/plugins-local/
      my-workflows/
        plugin.json
        <workflow-key>/
          workflow.json
          schemas/ processes/ rule_systems/ tools/ axes/ overlay/ seed/

Then restart the stack. Every directory with a `plugin.json` is validated and registered
at boot, and its workflows become selectable. Content is read-only to the platform: to
remove a pack, delete its directory here and restart.

Directory names must be lowercase letters, digits, `-` or `_`. `default` and `builtin`
are reserved for the packs that ship with the platform.

In Kubernetes this directory is not mounted by default — use the admin upload, or mount a
ConfigMap/PVC at `/app/plugins-local` (see `deploy/k8s/README.md`).
