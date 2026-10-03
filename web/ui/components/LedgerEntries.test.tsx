import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LedgerEntries } from "./LedgerEntries";
import { groupLines } from "@/lib/ledger";
import type { LedgerLine } from "@/lib/types";

it("renders immutable Cards context and partial cancellation in the shared ledger", () => {
  const base: LedgerLine = {
    id: 1, seq: 1, ts: 100, kind: "CARD_BUY", item: "Cards", category: "CARDS",
    currency: "gems", delta: -20, price: 20, balance_after: 480, observed: 500,
    dry_run: 0, run_id: null, visit: null, reason: null,
    detail: { quantity: 1, source: "automatic", program_revision: "saved-rev", goal_id: "acquire", budget_cycle_id: "cycle" },
  };
  const assignment: LedgerLine = { ...base, id: 2, seq: 2, kind: "CARD_ASSIGN",
    item: "Card equipment", currency: null, delta: 0, price: null, balance_after: null,
    detail: { result: "canceled", loadout_id: "farm", requested_equipped: ["cards.damage", "cards.health"],
      snapshot_before: { equipped: [] }, snapshot_after: { equipped: ["cards.damage"] } },
  };
  render(<LedgerEntries entries={groupLines([base, assignment])} kind={null} onKindChange={vi.fn()} />);
  for (const text of ["Quantity: 1", "Source: automatic", "Program revision: saved-rev", "Goal: acquire",
    "Budget cycle: cycle", "Loadout: farm", "Requested equipment: cards.damage, cards.health",
    "Equipment: cards.damage", "Result: canceled (partial application)"]) {
    expect(screen.getByText(text)).toBeInTheDocument();
  }
});
