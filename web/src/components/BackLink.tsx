import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

/** The audit found exactly ONE back link in the whole app. Detail and editor routes
 * use this so nobody is stranded on the browser's Back button. */
export function BackLink({ to, label }: { to: string; label: string }) {
  return (
    <Link
      to={to}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
    >
      <ArrowLeft className="h-3.5 w-3.5" />
      {label}
    </Link>
  );
}
