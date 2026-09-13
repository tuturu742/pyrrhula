import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/auth";

/** Router guard (T0.9): redirects to /login when there's no token. Mirrors the backend's
 * own rule (CLAUDE.md: "no route reachable without a resolved principal") on the frontend
 * side -- this doesn't replace server-side auth, it just avoids flashing protected UI.
 * The intended destination rides along so a deep link survives the sign-in detour. */
export function ProtectedRoute() {
  const token = useAuthStore((s) => s.token);
  const location = useLocation();
  if (!token) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ returnTo: location.pathname + location.search }}
      />
    );
  }
  return <Outlet />;
}
