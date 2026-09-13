import { useLabel } from "@/lib/vocabulary/useLabel";

export interface AttributeBlockProps {
  labelKey: string;
  value: number | string;
  /** Server-computed (F3.1's `modifier_source` contract, never evaluated client-side) --
   * `null` for an `attribute` field that isn't also tagged `modifier_source`. */
  modifier: number | null;
}

/** F3.4's `attribute`(+`modifier_source`) tags -- a strength score and its computed
 * modifier, or any other domain's "attribute with a derived adjustment", share this
 * one block. */
export function AttributeBlock({ labelKey, value, modifier }: AttributeBlockProps) {
  const t = useLabel();
  const sign = modifier !== null && modifier >= 0 ? "+" : "";

  return (
    <div
      className="flex flex-col items-center rounded-md border border-border px-3 py-2"
      data-widget="attribute_block"
    >
      <div className="text-xs text-muted-foreground">{t(labelKey)}</div>
      <div className="text-lg font-semibold">{value}</div>
      {modifier !== null && (
        <div className="text-xs text-muted-foreground">
          {sign}
          {modifier}
        </div>
      )}
    </div>
  );
}
