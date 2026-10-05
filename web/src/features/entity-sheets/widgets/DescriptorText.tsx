import { useFieldLabel } from "../fieldLabel";
import type { TextFieldProps } from "./IdentityText";

/** the `descriptor` tag -- free-text/enum flavour fields (a spell's school, a
 * ticket's priority) that are neither the thing's identity nor a bar/chip/meter. */
export function DescriptorText({ labelKey, value }: TextFieldProps) {
  const t = useFieldLabel();
  return (
    <div className="flex flex-col gap-0.5" data-widget="descriptor_text">
      <div className="text-xs text-muted-foreground">{t(labelKey)}</div>
      <div className="text-sm">{String(value)}</div>
    </div>
  );
}
