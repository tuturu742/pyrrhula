import type { EntityFieldView } from "./types";
import { widgetIdForTags } from "./tagWidgetRegistry";
import { ResourceBar } from "./widgets/ResourceBar";
import { StatusChipRow } from "./widgets/StatusChipRow";
import { AttributeBlock } from "./widgets/AttributeBlock";
import { ProgressionMeter } from "./widgets/ProgressionMeter";
import { IdentityText } from "./widgets/IdentityText";
import { DescriptorText } from "./widgets/DescriptorText";
import { RelationshipLink } from "./widgets/RelationshipLink";
import { HistoryChart } from "./widgets/HistoryChart";

export interface FieldWidgetProps {
  field: EntityFieldView;
  /** Every visible field on the entity, keyed by field key -- needed to resolve a
   * `resource` field's `max_ref` to another field's actual value. */
  fieldsByKey: Record<string, EntityFieldView>;
  entityId: string;
  labelKeyPrefix: string;
  showHistory?: boolean;
}

/**
 * The one dispatcher every field goes through: tag -> widget id (`tagWidgetRegistry`)
 * -> component. No domain knowledge anywhere in this file -- adding a pack never
 * touches it (F3.10's own acceptance criterion).
 */
export function FieldWidget({
  field,
  fieldsByKey,
  entityId,
  labelKeyPrefix,
  showHistory,
}: FieldWidgetProps) {
  const widgetId = widgetIdForTags(field.tags);
  const labelKey = `${labelKeyPrefix}.${field.key}`;

  switch (widgetId) {
    case "resource_bar": {
      const maxRef = field.tag_metadata.max_ref;
      const maxField = typeof maxRef === "string" ? fieldsByKey[maxRef] : undefined;
      const max = typeof maxField?.value === "number" ? maxField.value : null;
      const lowThreshold =
        typeof field.tag_metadata.low_threshold === "number" ? field.tag_metadata.low_threshold : null;
      return (
        <div>
          <ResourceBar
            labelKey={labelKey}
            value={typeof field.value === "number" ? field.value : 0}
            max={max}
            lowThreshold={lowThreshold}
          />
          {showHistory && <HistoryChart entityId={entityId} fieldPath={field.key} />}
        </div>
      );
    }
    case "status_chip_row":
      return (
        <StatusChipRow
          labelKey={labelKey}
          values={Array.isArray(field.value) ? (field.value as string[]) : []}
        />
      );
    case "attribute_block":
      return (
        <AttributeBlock
          labelKey={labelKey}
          value={field.value as number | string}
          modifier={typeof field.modifier === "number" ? field.modifier : null}
        />
      );
    case "progression_meter":
      return (
        <div>
          <ProgressionMeter
            labelKey={labelKey}
            value={typeof field.value === "number" ? field.value : 0}
            nextThreshold={null}
          />
          {showHistory && <HistoryChart entityId={entityId} fieldPath={field.key} />}
        </div>
      );
    case "identity_text":
      return <IdentityText labelKey={labelKey} value={field.value as string | number | boolean} />;
    case "relationship_link":
      return <RelationshipLink labelKey={labelKey} value={String(field.value)} />;
    case "descriptor_text":
    default:
      return <DescriptorText labelKey={labelKey} value={field.value as string | number | boolean} />;
  }
}
