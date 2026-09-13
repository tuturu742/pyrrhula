import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";

const STORAGE_KEY = "pyrrhula-theme";

const ThemeContext = createContext<{
  preference: ThemePreference;
  setPreference: (next: ThemePreference) => void;
  /** the theme actually applied right now (system resolved) */
  resolved: "light" | "dark";
}>({ preference: "system", setPreference: () => undefined, resolved: "light" });

function systemPrefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function apply(preference: ThemePreference): "light" | "dark" {
  const resolved =
    preference === "system" ? (systemPrefersDark() ? "dark" : "light") : preference;
  document.documentElement.classList.toggle("dark", resolved === "dark");
  return resolved;
}

/** Class-driven theming over the token set in index.css. `system` follows the OS
 * live (media-query listener), an explicit choice persists in localStorage. */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(
    () => (localStorage.getItem(STORAGE_KEY) as ThemePreference | null) ?? "system",
  );
  const [resolved, setResolved] = useState<"light" | "dark">(() => apply(preference));

  const setPreference = useCallback((next: ThemePreference) => {
    localStorage.setItem(STORAGE_KEY, next);
    setPreferenceState(next);
    setResolved(apply(next));
  }, []);

  useEffect(() => {
    if (preference !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setResolved(apply("system"));
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [preference]);

  return (
    <ThemeContext.Provider value={{ preference, setPreference, resolved }}>
      {children}
    </ThemeContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTheme() {
  return useContext(ThemeContext);
}
