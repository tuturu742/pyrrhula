import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * A dark-theme dropdown was white text on a white popup — unreadable until the hover
 * highlight passed over an entry.
 *
 * The cause was not a missing token. A `<select>`'s option list is painted by the
 * browser, not by our CSS, and the browser decides light or dark from `color-scheme`.
 * We set `.dark` on the root element and never declared it, so the browser went on
 * assuming light while the options inherited the near-white dark foreground.
 *
 * Asserted against the stylesheet rather than a rendered component on purpose: jsdom
 * does not paint native controls, so a DOM test would pass here no matter what the CSS
 * said. This keeps the declaration from being dropped again, which is the realistic
 * regression — it is one line with no visible owner.
 */
const css = readFileSync(resolve(__dirname, "../index.css"), "utf8");

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, `${selector} block not found`).toBeGreaterThan(-1);
  return css.slice(start, css.indexOf("}", start));
}

describe("native control theming", () => {
  it("test_both_themes_declare_a_color_scheme", () => {
    expect(block(":root")).toMatch(/color-scheme:\s*light/);
    expect(block(".dark")).toMatch(/color-scheme:\s*dark/);
  });

  it("test_option_lists_use_the_popover_tokens", () => {
    // Where the browser honours it, the dropdown matches the app rather than the
    // browser's generic dark grey.
    expect(css).toMatch(/select option[\s\S]{0,120}background-color:\s*var\(--popover\)/);
    expect(css).toMatch(/select option[\s\S]{0,160}color:\s*var\(--popover-foreground\)/);
  });
});
