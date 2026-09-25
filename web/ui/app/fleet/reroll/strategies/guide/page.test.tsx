import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import StrategyGuidePage from "./page";
import { GUIDE_BLOCKS } from "../strategyBlocks";

vi.mock("./guide.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));
vi.mock("next/link", () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));

test("guide explains the loop and every block with an anchor and a diagram", () => {
  const { container } = render(<StrategyGuidePage />);
  expect(screen.getByRole("heading", { name: /how strategies decide/i })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: /decision loop/i })).toBeInTheDocument();
  for (const block of GUIDE_BLOCKS) {
    const section = container.querySelector(`#block-${block.type}`);
    expect(section, block.type).not.toBeNull();
    expect(section!.querySelector("svg[role=img]"), block.type).not.toBeNull();
  }
});

test("walkthrough scenarios highlight the active block and reason", () => {
  render(<StrategyGuidePage />);
  fireEvent.click(screen.getByRole("button", { name: "Saving for Thorns" }));
  expect(screen.getByText(/cheap defense/i, { selector: "[data-active=true] *" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/80% of the Thorns price/);
});

test("guide links back to the studio", () => {
  render(<StrategyGuidePage />);
  // Ruling: repo uses trailingSlash: true (web/ui/next.config), so internal links must keep the trailing slash.
  expect(screen.getByRole("link", { name: /back to strategy studio/i })).toHaveAttribute("href", "/fleet/reroll/strategies/");
});
