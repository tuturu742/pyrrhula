import { Link } from "react-router-dom";

/** The `*` route. A silent redirect to `/` used to swallow typos and dead links --
 * indistinguishable from clicking "Workspaces". Saying "not found" is kinder. */
export function NotFoundPage() {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-24 text-center">
      <p className="text-5xl font-semibold tracking-tight text-muted-foreground">404</p>
      <h1 className="text-xl font-semibold">There's nothing at this address.</h1>
      <p className="text-sm text-muted-foreground">
        The link may be stale, or the thing it pointed at was archived.
      </p>
      <Link
        to="/"
        className="rounded-md border border-border px-4 py-2 text-sm hover:bg-accent"
      >
        Back to your Workspaces
      </Link>
    </div>
  );
}
