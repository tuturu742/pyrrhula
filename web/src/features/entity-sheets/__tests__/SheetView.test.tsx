import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SheetView } from "../SheetView";
import type { EntityView } from "../types";

function makeEntity(overrides: Partial<EntityView> = {}): EntityView {
  return {
    id: "entity-1",
    key: "hero-1",
    name: "A Hero",
    schema_id: "schema-1",
    version: 1,
    fields: [
      { key: "name", type: "string", value: "Visible Name", tags: ["identity"], tag_metadata: {} },
    ],
    derived: {},
    fsm_states: {},
    views: [],
    ...overrides,
  };
}

describe("SheetView", () => {
  it("test_scope_filtered_fields_are_indistinguishable_from_absent", () => {
    // A field the viewer can't see is never present in the API response at all (the
    // backend's own contract) -- this proves the client-side rendering
    // side of that same contract: nothing renders for a field that was never sent,
    // there is no separate "hidden" placeholder/blank state to distinguish from
    // "doesn't exist".
    const entity = makeEntity();
    render(<SheetView entity={entity} labelKeyPrefix="schema.test" />);

    expect(screen.getByText("Visible Name")).toBeInTheDocument();
    expect(screen.queryByText(/secret/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId("hidden-field")).not.toBeInTheDocument();
  });

  it("renders every field via the generic dispatcher across different tag sets", () => {
    const entity = makeEntity({
      fields: [
        { key: "hp", type: "integer", value: 10, tags: ["resource"], tag_metadata: { max_ref: "max_hp", low_threshold: 3 } },
        { key: "max_hp", type: "integer", value: 20, tags: ["identity"], tag_metadata: {} },
        { key: "strength", type: "integer", value: 16, tags: ["attribute", "modifier_source"], tag_metadata: {}, modifier: 3 },
        { key: "conditions", type: "array", value: ["poisoned"], tags: ["status_set"], tag_metadata: {} },
      ],
    });
    render(<SheetView entity={entity} labelKeyPrefix="schema.test" />);

    expect(screen.getByText("10 / 20")).toBeInTheDocument();
    expect(screen.getByText("16")).toBeInTheDocument();
    expect(screen.getByText("+3")).toBeInTheDocument();
    expect(screen.getByText("poisoned")).toBeInTheDocument();
  });

  it("puts a field the ViewDef doesn't mention into a default group, not nowhere", () => {
    const entity = makeEntity({
      fields: [
        { key: "name", type: "string", value: "Visible", tags: ["identity"], tag_metadata: {} },
        { key: "orphan_field", type: "string", value: "Still Here", tags: ["descriptor"], tag_metadata: {} },
      ],
      views: [
        {
          key: "main_view",
          groups: [{ key: "main", label_key: "group.main", field_keys: ["name"] }],
          tabs: [],
        },
      ],
    });
    render(<SheetView entity={entity} labelKeyPrefix="schema.test" />);

    expect(screen.getByText("Visible")).toBeInTheDocument();
    expect(screen.getByText("Still Here")).toBeInTheDocument(); // never silently dropped
  });
});
