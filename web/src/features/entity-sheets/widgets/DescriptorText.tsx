import { useLabel } from "@/lib/vocabulary/useLabel";
import type { TextFieldProps } from "./IdentityText";

/** F3.4's `descriptor` tag -- free-text/enum flavour fields (a spell's school, a
 * ticket's priority) that are neither the thing's identity nor a bar/chip/meter. */
export function DescriptorText({ labelKey, value }: TextFieldProps) {
  const t = useLabel();
  return (
    <div className="flex flex-col gap-0.5" data-widget="descriptor_text">
      <div className="text-xs text-muted-foreground">{t(labelKey)}</div>
      <div className="text-sm">{String(value)}</div>
    </div>
  );
}
