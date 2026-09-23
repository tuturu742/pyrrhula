import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

const FEATURE_DIR = path.resolve(__dirname, "..");

// Every file this feature is allowed to contain -- one component per core tag
// contract (F3.4), plus the generic dispatcher/layout/data-fetching plumbing. A file
// named after a pack concept (e.g. "CharacterSheet.tsx", "TicketView.tsx") would mean
// domain knowledge leaked into this folder.
const ALLOWED_FILES = new Set([
  "api.ts",
  "types.ts",
  "tagWidgetRegistry.ts",
  "FieldWidget.tsx",
  "SheetView.tsx",
  "EntitySheetContainer.tsx",
  "EntitySheetPage.tsx", // route mount -- generic, takes ids from the URL
  "EntityListPage.tsx", // route mount -- lists a workspace's entities by schema key
  "widgets/ResourceBar.tsx",
  "widgets/StatusChipRow.tsx",
  "widgets/AttributeBlock.tsx",
  "widgets/ProgressionMeter.tsx",
  "widgets/IdentityText.tsx",
  "widgets/DescriptorText.tsx",
  "widgets/RelationshipLink.tsx",
  "widgets/HistoryChart.tsx",
]);

function listFiles(dir: string, prefix = ""): string[] {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    if (entry.name === "__tests__") continue;
    const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      files.push(...listFiles(path.join(dir, entry.name), relPath));
    } else {
      files.push(relPath);
    }
  }
  return files;
}

describe("entity-sheets feature contains no domain-specific components", () => {
  it("test_sheet_feature_contains_no_domain_specific_components", () => {
    const files = listFiles(FEATURE_DIR);
    const unexpected = files.filter((f) => !ALLOWED_FILES.has(f));
    expect(unexpected).toEqual([]);
  });
});
