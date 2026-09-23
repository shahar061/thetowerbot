import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import ArchivesPage from "./page";

vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ choose: vi.fn(), accounts: [
  { key: "worker:A_1", account_id: "111", instance: "A_1", kind: "worker", running: false, dashboard_url: null, run_numbers: [1] },
  { key: "worker:A_2", account_id: "222", instance: "A_2", kind: "worker", running: false, dashboard_url: null, run_numbers: [1, 2] },
  { key: "unattributed", account_id: null, instance: null, kind: "unattributed", running: false, dashboard_url: null },
] }) }));

test("filters archived accounts by reroll, starting from the ?run= link", () => {
  window.history.replaceState(null, "", "/archives/?run=2");
  render(<ArchivesPage />);
  expect(screen.getByText("Tower account 222")).toBeInTheDocument();
  expect(screen.queryByText("Tower account 111")).toBeNull();
  fireEvent.change(screen.getByRole("combobox", { name: "Reroll" }), { target: { value: "all" } });
  expect(screen.getByText("Tower account 111")).toBeInTheDocument();
  expect(screen.getByText("A_1 · registered worker · bot not running · Reroll #1")).toBeInTheDocument();
});
