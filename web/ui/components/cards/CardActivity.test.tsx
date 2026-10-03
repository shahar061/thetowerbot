import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CardActivity } from "./CardActivity";
import type { CardOperation } from "@/lib/cards";
it("shows queued work as pending and exposes cancel without implying a purchase", () => {
  render(
    <CardActivity
      operations={[
        {
          operation_id: "pending",
          command: { kind: "refresh" },
          created_at: 100,
          status: "queued",
          reason: "paused",
          spent_gems: null,
          rewards: [],
        } as unknown as CardOperation,
      ]}
      onCancel={vi.fn()}
    />,
  );
  expect(screen.getByText("refresh · queued")).toBeInTheDocument();
  expect(screen.getByText("paused")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Cancel operation" }),
  ).toBeEnabled();
  expect(screen.queryByText(/Gem spend/)).not.toBeInTheDocument();
});
it("shows a verified explicit clear as empty equipment, not unknown", () => {
  render(
    <CardActivity
      operations={[
        {
          operation_id: "clear",
          command: { kind: "clear" },
          created_at: 100,
          status: "confirmed",
          reason: null,
          spent_gems: 0,
          rewards: [],
          snapshot_after: { equipment_complete: true, equipped: [] },
        } as unknown as CardOperation,
      ]}
      onCancel={vi.fn()}
    />,
  );
  expect(
    screen.getByText("Equipment after: None (verified clear)"),
  ).toBeInTheDocument();
});
it("navigates to recorded operation evidence without fabricating image URLs or pending copy", () => {
  const inspect = vi.fn();
  const operation = {
    operation_id: "proof",
    command: { kind: "apply" },
    created_at: 100,
    status: "confirmed",
    reason: null,
    spent_gems: 0,
    rewards: [],
    snapshot_after: {
      observed_at: 100,
      equipment_complete: true,
      equipped: ["cards.damage"],
      frame_digest: "digest123",
      items: [{ card_id: "cards.damage", evidence_ref: "digest123:0:0" }],
      equipment_evidence: {
        observed_at: 100,
        evidence_ref: "equipment-digest",
      },
    },
  } as unknown as CardOperation;
  render(
    <CardActivity
      operations={[operation]}
      onCancel={vi.fn()}
      onInspectEvidence={inspect}
      inspectedOperation={operation}
    />,
  );
  fireEvent.click(
    screen.getByRole("button", { name: "View recorded evidence" }),
  );
  expect(inspect).toHaveBeenCalledWith("proof");
  expect(
    screen.getByRole("region", { name: "Recorded operation evidence" }),
  ).toHaveTextContent("digest123:0:0");
  expect(screen.getByText(/No persisted image URL/)).toBeInTheDocument();
  expect(
    screen.queryByText("Awaiting worker processing"),
  ).not.toBeInTheDocument();
});
it.each(["queued", "blocked", "canceled"])("does not imply reconciliation for an undispatched %s buy", (status) => {
  render(<CardActivity operations={[{ operation_id: status, command: { kind: "buy" }, created_at: 100,
    status, transaction_key: null, spent_gems: null, rewards: [] } as unknown as CardOperation]} onCancel={vi.fn()} />);
  expect(screen.getByText("Gem spend: Unknown / not recorded")).toBeInTheDocument();
  expect(screen.queryByText(/pending reconciliation/)).not.toBeInTheDocument();
});
it.each(["dispatched", "verifying", "reconciliation_required"])("retains reconciliation guidance for an uncertain %s buy", (status) => {
  render(<CardActivity operations={[{ operation_id: status, command: { kind: "buy" }, created_at: 100,
    status, transaction_key: "receipt", spent_gems: null, rewards: [] } as unknown as CardOperation]} onCancel={vi.fn()} />);
  expect(screen.getByText("Gem spend: Unknown / not recorded — pending reconciliation")).toBeInTheDocument();
});
