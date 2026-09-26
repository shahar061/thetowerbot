import { render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { StrategyLedgerEntry } from "@/lib/strategyStudio";
import { StrategyHistory } from "./StrategyHistory";

vi.mock("./studio.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));

const at = Date.UTC(2026, 8, 25, 14, 30) / 1000;
const entries: StrategyLedgerEntry[] = [
  { kind: "reassigned", at, route_revision: 3, actor: "operator", worker: "Tiramisu64_45", account_id: "ACC-B",
    before: { strategy_id: "turtle", strategy_version: 1, strategy_name: "Turtle" },
    after: { strategy_id: "strategy-x", strategy_version: 2, strategy_name: "Turtle Discord Guide" } },
  { kind: "saved", at: at - 60, strategy_id: "strategy-x", strategy_name: "Turtle Discord Guide", strategy_version: 2,
    source_template: "turtle" },
  { kind: "unassigned", at: at - 120, route_revision: 2, actor: "strategy_studio", worker: "Air_38", account_id: "ACC-A",
    before: { strategy_id: "turtle", strategy_version: 1, strategy_name: "Turtle" }, after: null },
];

test("lists each change with its time, kind, worker, versions, actor and revision", () => {
  render(<StrategyHistory entries={entries} error="" />);
  const history = screen.getByRole("region", { name: "History" });
  const rows = within(history).getAllByRole("listitem");
  expect(rows).toHaveLength(3);
  expect(within(rows[0]).getByText("Reassigned")).toBeInTheDocument();
  expect(within(rows[0]).getByText("Tiramisu64_45")).toBeInTheDocument();
  expect(within(rows[0]).getByText("Turtle v1 → Turtle Discord Guide v2")).toBeInTheDocument();
  expect(within(rows[0]).getByText("by operator · route rev 3")).toBeInTheDocument();
  // Local time, as the operator's own clock reads it.
  expect(within(rows[0]).getByText(new Date(at * 1000).toLocaleString())).toBeInTheDocument();
  expect(within(rows[1]).getByText("Saved")).toBeInTheDocument();
  expect(within(rows[1]).getByText("Turtle Discord Guide v2")).toBeInTheDocument();
  expect(within(rows[1]).getByText("from turtle")).toBeInTheDocument();
  expect(within(rows[2]).getByText("Unassigned")).toBeInTheDocument();
  expect(within(rows[2]).getByText("Turtle v1 → none")).toBeInTheDocument();
});

test("says so when nothing has changed yet, and when history cannot be read", () => {
  const { rerender } = render(<StrategyHistory entries={[]} error="" />);
  expect(screen.getByText("No strategy changes yet")).toBeInTheDocument();
  rerender(<StrategyHistory entries={null} error="Service unavailable" />);
  expect(screen.getByRole("alert")).toHaveTextContent("History unavailable: Service unavailable");
});
