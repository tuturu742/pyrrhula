import { useFieldLabel } from "../fieldLabel";

export interface TextFieldProps {
  labelKey: string;
  value: string | number | boolean;
}

/** the `identity` tag -- a name, a title, whatever the pack calls the "what is
 * this thing" field. */
export function IdentityText({ labelKey, value }: TextFieldProps) {
  const t = useFieldLabel();
  return (
    <div className="flex flex-col gap-0.5" data-widget="identity_text">
      <div className="text-xs text-muted-foreground">{t(labelKey)}</div>
      <div className="text-sm font-medium">{String(value)}</div>
    </div>
  );
}
