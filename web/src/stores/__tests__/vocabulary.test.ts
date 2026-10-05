import { beforeEach, describe, expect, it } from "vitest";
import { DEFAULT_LABELS } from "@/lib/vocabulary/labels";
import { useVocabularyStore } from "@/stores/vocabulary";
import { humanizeFieldKey } from "@/features/entity-sheets/fieldLabel";

const TENANT = { "entity.workspace": "Project" };
const WORKSPACE = { "entity.workspace": "World / Campaign" };

describe("vocabulary store layering", () => {
  beforeEach(() => {
    useVocabularyStore.setState({
      overlayKey: "rpg_v1",
      labels: DEFAULT_LABELS,
      tenant: null,
      workspace: null,
    });
  });

  it("shows the tenant default once loaded, with no workspace in scope", () => {
    useVocabularyStore.getState().setTenantOverlay("swdev_v1", TENANT);
    expect(useVocabularyStore.getState().overlayKey).toBe("swdev_v1");
    expect(useVocabularyStore.getState().labels["entity.workspace"]).toBe("Project");
  });

  it("lets a workspace's overlay win while it is in scope, whichever loads last", () => {
    const store = useVocabularyStore.getState();
    store.setWorkspaceOverlay("rpg_v1", WORKSPACE);
    store.setTenantOverlay("swdev_v1", TENANT);
    expect(useVocabularyStore.getState().overlayKey).toBe("rpg_v1");
    expect(useVocabularyStore.getState().labels["entity.workspace"]).toBe("World / Campaign");
  });

  it("falls back to the tenant default when the workspace page unmounts", () => {
    const store = useVocabularyStore.getState();
    store.setTenantOverlay("swdev_v1", TENANT);
    store.setWorkspaceOverlay("rpg_v1", WORKSPACE);
    store.clearWorkspaceOverlay();
    expect(useVocabularyStore.getState().overlayKey).toBe("swdev_v1");
    expect(useVocabularyStore.getState().labels["entity.workspace"]).toBe("Project");
  });

  it("a live switch replaces the layer that is active", () => {
    const store = useVocabularyStore.getState();
    store.setTenantOverlay("swdev_v1", TENANT);
    store.setOverlay("default_v1", { "entity.workspace": "Workspace" });
    expect(useVocabularyStore.getState().tenant?.key).toBe("default_v1");
    store.setWorkspaceOverlay("rpg_v1", WORKSPACE);
    store.setOverlay("default_v1", { "entity.workspace": "Workspace" });
    expect(useVocabularyStore.getState().workspace?.key).toBe("default_v1");
    expect(useVocabularyStore.getState().tenant?.key).toBe("default_v1");
  });
});

describe("humanizeFieldKey", () => {
  it("reads the last segment of a field key", () => {
    expect(humanizeFieldKey("sheet.max_hit_points")).toBe("Max hit points");
    expect(humanizeFieldKey("sheet.strength")).toBe("Strength");
    expect(humanizeFieldKey("schema.test.character-class")).toBe("Character class");
  });
});
