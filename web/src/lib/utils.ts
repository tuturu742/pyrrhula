import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

// shadcn/ui's standard className helper -- `npx shadcn add <component>` expects this to
// exist at this path (see components.json's "utils" alias).
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
