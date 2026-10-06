import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "../Markdown";

describe("Markdown", () => {
  it("renders emphasis, lists and inline code instead of showing the markup", () => {
    const { container } = render(
      <Markdown text={"**Review (round 2)** approved\n\n1. merge `#11`\n2. deploy"} />,
    );
    expect(screen.getByText("Review (round 2)").tagName).toBe("STRONG");
    expect(container.querySelectorAll("ol > li")).toHaveLength(2);
    expect(screen.getByText("#11").tagName).toBe("CODE");
    expect(container.textContent).not.toContain("**");
  });

  it("keeps raw HTML from the model as text, never as markup", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> <b>bold</b>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("drops javascript: links and opens real ones in a new tab", () => {
    const { container } = render(
      <Markdown text={"[bad](javascript:alert(1)) and [good](https://github.com/tuturu742/pyrrhula)"} />,
    );
    const links = [...container.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href") ?? "")).not.toContain("javascript:alert(1)");
    const good = links.find((a) => a.textContent === "good");
    expect(good?.getAttribute("href")).toBe("https://github.com/tuturu742/pyrrhula");
    expect(good?.getAttribute("target")).toBe("_blank");
    expect(good?.getAttribute("rel")).toContain("noopener");
  });
});
