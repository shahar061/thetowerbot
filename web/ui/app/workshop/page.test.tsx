import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import WorkshopPage from "./page";
const state = vi.hoisted(() => ({ selected: { key: "worker:A", account_id: "account-A", instance: "A", running: true } as Record<string, unknown> | null }));
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => state }));
vi.mock("@/app/fleet/reroll/workshop/WorkshopViews", () => ({ WorkshopViews: ({ members }: { members: unknown[] }) => <pre data-testid="workshop">{JSON.stringify(members)}</pre> }));
test("scopes the reused workshop to the selected emulator and switches accounts", () => {
 const view = render(<WorkshopPage />);
 expect(screen.getByTestId("workshop")).toHaveTextContent('"account_key":"worker:A"');
 state.selected = { key: "worker:B", account_id: "account-B", instance: "B", running: false };
 view.rerender(<WorkshopPage />);
 expect(screen.getByTestId("workshop")).toHaveTextContent('"account_id":"account-B"');
 expect(screen.getByTestId("workshop")).not.toHaveTextContent("account-A");
 state.selected = null;
 view.rerender(<WorkshopPage />);
 expect(screen.queryByTestId("workshop")).toBeNull();
 expect(screen.getByText(/Select an emulator/)).toBeInTheDocument();
});
