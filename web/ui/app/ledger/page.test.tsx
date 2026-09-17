// fireEvent, not @testing-library/user-event: user-event is not a
// dependency of this project and every other test here uses fireEvent.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe as group, expect, it, vi } from "vitest";
import LedgerPage from "./page";

const { fetchLedger } = vi.hoisted(() => ({ fetchLedger: vi.fn() }));

vi.mock("@/lib/api", () => ({ fetchLedger }));
vi.mock("@/lib/useEventStream", () => ({ useEventStream: () => ({ events: [], connected: true }) }));

function payload(overrides = {}) {
  return {
    lines: [], balances: { coins: null, gems: null }, rehearsals: 0, next: null,
    ...overrides,
  };
}

const A_PURCHASE = {
  id: 1, seq: 1, ts: 0, kind: "WORKSHOP_BUY", item: "Health",
  category: "DEFENSE", currency: "coins", delta: -75, price: 75,
  balance_after: 1695, observed: 1770, dry_run: 0, run_id: null, visit: 1,
  reason: null, detail: {},
};

group("LedgerPage", () => {
  it("shows an explicit empty state with nothing recorded", async () => {
    fetchLedger.mockResolvedValue(payload());
    render(<LedgerPage />);

    await screen.findByText("Nothing recorded yet.");
  });

  it("does not report an unreachable server as an empty account", async () => {
    fetchLedger.mockRejectedValue(new Error("/api/ledger -> 503"));
    render(<LedgerPage />);

    await screen.findByText(/could not load/i);
    expect(screen.queryByText("Nothing recorded yet.")).toBeNull();
  });

  it("lists a purchase with its delta and running balance", async () => {
    fetchLedger.mockResolvedValue(
      payload({ lines: [A_PURCHASE], balances: { coins: 1695, gems: 40 } }),
    );
    render(<LedgerPage />);

    await screen.findByText("Health");
    expect(screen.getByText("-75")).toBeDefined();
    expect(screen.getByText("1,695")).toBeDefined();
  });

  it("flags an unexplained line as the account moving outside the bot", async () => {
    fetchLedger.mockResolvedValue(payload({
      lines: [{
        ...A_PURCHASE, id: 2, seq: null, kind: "UNEXPLAINED", item: null,
        currency: "gems", delta: -140, price: null, balance_after: 40,
        observed: 40,
      }],
    }));
    render(<LedgerPage />);

    await screen.findByText("UNEXPLAINED");
    expect(screen.getByText(/outside the bot/i)).toBeDefined();
  });

  it("does not mark a non-financial line as an unknown amount", async () => {
    fetchLedger.mockResolvedValue(payload({
      lines: [{
        ...A_PURCHASE, id: 3, kind: "VISIT_END", item: null, currency: null,
        delta: null, price: null, balance_after: null, observed: null,
      }],
    }));
    render(<LedgerPage />);

    await screen.findByText("VISIT_END");
    // "?" means "moved by an unknown amount" and a visit boundary moved
    // nothing at all.
    expect(screen.queryByText("?")).toBeNull();
  });

  it("hides rehearsals until asked, and says how many there are", async () => {
    fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE], rehearsals: 3 }));
    render(<LedgerPage />);

    const toggle = await screen.findByRole("button", { name: /show rehearsals \(3\)/i });
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(fetchLedger).toHaveBeenLastCalledWith({ includeRehearsals: true }),
    );
  });

  it("asks the route for one kind when a chip is picked, and drops it again", async () => {
    fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE] }));
    render(<LedgerPage />);

    const chip = await screen.findByRole("button", { name: "workshop buy" });
    fireEvent.click(chip);

    await waitFor(() =>
      expect(fetchLedger).toHaveBeenLastCalledWith({ kind: "WORKSHOP_BUY" }),
    );

    // The route takes a single kind, so the active chip is a toggle: clicking
    // it again means no filter, not an empty one.
    fireEvent.click(screen.getByRole("button", { name: "workshop buy" }));
    await waitFor(() => expect(fetchLedger).toHaveBeenLastCalledWith({}));
  });

  it("filters by the kind tag on a ledger row", async () => {
    fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE] }));
    render(<LedgerPage />);

    fireEvent.click(await screen.findByRole("button", { name: "Filter WORKSHOP_BUY" }));
    await waitFor(() =>
      expect(fetchLedger).toHaveBeenLastCalledWith({ kind: "WORKSHOP_BUY" }),
    );
  });

  it("asks the route for one currency, and sends none for all", async () => {
    fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE] }));
    render(<LedgerPage />);

    fireEvent.click(await screen.findByRole("button", { name: "gems" }));
    await waitFor(() =>
      expect(fetchLedger).toHaveBeenLastCalledWith({ currency: "gems" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "all" }));
    await waitFor(() => expect(fetchLedger).toHaveBeenLastCalledWith({}));
  });

  it("offers no way to page an account whose history is already whole", async () => {
    fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE], next: null }));
    render(<LedgerPage />);

    await screen.findByText("Health");
    expect(screen.queryByRole("button", { name: /load more/i })).toBeNull();
  });

  it("appends the next page rather than replacing the lines on screen", async () => {
    const older = { ...A_PURCHASE, id: 9, item: "Damage", balance_after: 1620 };
    fetchLedger
      .mockResolvedValueOnce(payload({ lines: [A_PURCHASE], next: 1 }))
      .mockResolvedValueOnce(payload({ lines: [older], next: null }));
    render(<LedgerPage />);

    fireEvent.click(await screen.findByRole("button", { name: /load more/i }));

    await screen.findByText("Damage");
    // The first page is still there - a history that replaces itself as you
    // page through it is not a history.
    expect(screen.getByText("Health")).toBeDefined();
    expect(fetchLedger).toHaveBeenLastCalledWith({ before: 1 });
    // ...and the last page says so by taking the control away.
    expect(screen.queryByRole("button", { name: /load more/i })).toBeNull();
  });
});
