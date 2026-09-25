import { fireEvent, render, screen, within } from "@testing-library/react";
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
  // Both walkthroughs render their own always-live role="status" reason paragraph, so the
  // query is scoped to the "Turtle · Workshop" region (its <section aria-label> gives an
  // implicit region role) to keep it unambiguous.
  const workshop = screen.getByRole("region", { name: /Turtle · Workshop/i });
  fireEvent.click(within(workshop).getByRole("button", { name: "Saving for Thorns" }));
  expect(within(workshop).getByText(/cheap defense/i, { selector: "[data-active=true] *" })).toBeInTheDocument();
  expect(within(workshop).getByRole("status")).toHaveTextContent(/80% of the Thorns price/);
});

test("Turtle objectives step lists the economy tail after Thorns", () => {
  render(<StrategyGuidePage />);
  const workshop = screen.getByRole("region", { name: /Turtle · Workshop/i });
  expect(within(workshop).getByText("Unlock Defense → Def. Abs (5) → Unlock Thorns → Thorns → 51%, then Cash Bonus · Coins/Kill · Health")).toBeInTheDocument();
});

test("guide links back to the studio", () => {
  render(<StrategyGuidePage />);
  // Ruling: repo uses trailingSlash: true (web/ui/next.config), so internal links must keep the trailing slash.
  expect(screen.getByRole("link", { name: /back to strategy studio/i })).toHaveAttribute("href", "/fleet/reroll/strategies/");
});

test("every block card's worked example includes a number", () => {
  const { container } = render(<StrategyGuidePage />);
  for (const block of GUIDE_BLOCKS) {
    const section = container.querySelector(`#block-${block.type}`);
    const example = section!.querySelector(".example");
    expect(example, block.type).not.toBeNull();
    expect(example!.textContent, block.type).toMatch(/\d/);
  }
});
