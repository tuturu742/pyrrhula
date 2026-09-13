# web/ — the Pyrrhula frontend

React 18 + Vite + TypeScript. Tailwind v4 (CSS-config in `src/index.css`) with the
shadcn/ui token set; TanStack Query for data, Zustand for the two small stores
(auth, vocabulary), React Flow for the process/schema/repo canvases.

- `src/App.tsx` — all routes; `src/components/AppShell.tsx` — the shell.
- `src/features/*` — one directory per product area.
- `src/lib/api-client/` — typed client generated from the API's OpenAPI schema:
  run the api locally and `pnpm exec openapi-typescript http://localhost:8000/openapi.json -o src/lib/api-client/schema.ts`.
- Vocabulary: user-facing nouns go through `useLabel()` and the tenant's overlay —
  never hardcode domain words (see CLAUDE.md rule 1).

Dev: `pnpm dev` (proxies `/api` to :8000, see `vite.config.ts`). Build: `pnpm build`.
Tests: `pnpm test`. Desktop-first by design; the minimum comfortable width is ~900px.
