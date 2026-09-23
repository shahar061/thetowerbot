// fireEvent, not @testing-library/user-event: user-event is not a
// dependency of this project and every other test here uses fireEvent.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe as group, expect, it, vi } from "vitest";
import LedgerPage from "./page";

// `stream.events` is mutable so a test can push an event and re-render; every
// other test sees an empty feed, exactly as before.
const { fetchLedger, stream } = vi.hoisted(() => ({
  fetchLedger: vi.fn(),
  stream: { events: [] as { type: string; seq: number }[] },
}));

vi.mock("@/lib/api", () => ({ fetchLedger }));
vi.mock("@/lib/useEventStream", () => ({
  useEventStream: () => ({ events: stream.events, connected: true }),
}));

beforeEach(() => {
  stream.events = [];
});

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
    // Scoped to the table: the header now shows every balance as its own
    // number too, and "1,695" there is the header, not this row.
    const table = screen.getByRole("table");
    expect(within(table).getByText("-75")).toBeDefined();
    expect(within(table).getByText("1,695")).toBeDefined();
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
    // Its own seq: spreading A_PURCHASE would inherit seq 1 and claim to be
    // the same event as the Health purchase, which the page now (correctly)
    // shows as one row.
    const older = { ...A_PURCHASE, id: 9, seq: 9, item: "Damage", balance_after: 1620 };
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
  group("rewards in more than one currency", () => {
    // Two lines, one event: exactly what classify() stores for a mission
    // claim. Newest first, so the gem line (higher id) arrives first.
    const COINS_LINE = {
      ...A_PURCHASE, id: 19, seq: 501, kind: "MISSION_CLAIM", item: "Kill 1,000 enemies",
      category: "MISSIONS", currency: "coins", delta: 120, price: null,
      balance_after: 6318, observed: null, visit: null,
    };
    const GEMS_LINE = { ...COINS_LINE, id: 20, currency: "gems", delta: 5, balance_after: 68 };

    it("shows one claim paying coins and gems as one row, not two", async () => {
      fetchLedger.mockResolvedValue(payload({ lines: [GEMS_LINE, COINS_LINE] }));
      render(<LedgerPage />);

      await screen.findByText("Kill 1,000 enemies");
      expect(screen.getAllByText("Kill 1,000 enemies")).toHaveLength(1);
      const row = screen.getByText("Kill 1,000 enemies").closest("tr")!;
      expect(within(row).getByText("+120")).toBeDefined();
      expect(within(row).getByText("+5")).toBeDefined();
    });

    it("names the currency on every amount, not only on the balance", async () => {
      fetchLedger.mockResolvedValue(payload({ lines: [GEMS_LINE, COINS_LINE] }));
      render(<LedgerPage />);

      await screen.findByText("Kill 1,000 enemies");
      // +120 and +5 used to be indistinguishable; the amount now says which.
      expect(screen.getByTitle("+120 coins")).toBeDefined();
      expect(screen.getByTitle("+5 gems")).toBeDefined();
    });

    it("keeps an unread amount distinct from a read one in the same reward", async () => {
      fetchLedger.mockResolvedValue(payload({
        lines: [GEMS_LINE, { ...COINS_LINE, delta: null, balance_after: null }],
      }));
      render(<LedgerPage />);

      await screen.findByText("Kill 1,000 enemies");
      expect(screen.getByTitle("unknown amount of coins")).toBeDefined();
      expect(screen.getByTitle("+5 gems")).toBeDefined();
    });

    it("rejoins a reward that Load more split across two pages", async () => {
      fetchLedger
        .mockResolvedValueOnce(payload({ lines: [GEMS_LINE], next: 20 }))
        .mockResolvedValueOnce(payload({ lines: [COINS_LINE], next: null }));
      render(<LedgerPage />);

      fireEvent.click(await screen.findByRole("button", { name: /load more/i }));

      await screen.findByText("+120");
      // Grouping runs over every page fetched, so the halves meet again.
      expect(screen.getAllByText("Kill 1,000 enemies")).toHaveLength(1);
    });

    it("never folds lines without a source event into one row", async () => {
      // Derived UNEXPLAINED lines carry seq null; grouping on null would
      // collapse every one of them together.
      const gap = { ...A_PURCHASE, seq: null, kind: "UNEXPLAINED", item: null, currency: "coins" };
      fetchLedger.mockResolvedValue(payload({
        lines: [{ ...gap, id: 31, delta: 85 }, { ...gap, id: 30, delta: -40 }],
      }));
      render(<LedgerPage />);

      await screen.findByText("+85");
      expect(screen.getAllByRole("button", { name: "Filter UNEXPLAINED" })).toHaveLength(2);
    });
  });

  group("currencies beyond coins and gems", () => {
    const STONES = {
      ...A_PURCHASE, id: 40, seq: 700, kind: "MILESTONE_CLAIM", item: "Tier 2 Wave 50",
      category: null, currency: "stones", delta: 10, price: null,
      balance_after: null, observed: null, visit: null,
    };

    it("offers a filter for every currency the history holds", async () => {
      fetchLedger.mockResolvedValue(payload({
        lines: [STONES], currencies: ["coins", "stones"],
        balances: { coins: 1695, gems: null, stones: null }, balanced: ["coins", "gems"],
      }));
      render(<LedgerPage />);

      fireEvent.click(await screen.findByRole("button", { name: "stones" }));
      await waitFor(() =>
        expect(fetchLedger).toHaveBeenLastCalledWith({ currency: "stones" }),
      );
    });

    it("says a stones balance is not tracked rather than showing an empty wallet", async () => {
      fetchLedger.mockResolvedValue(payload({
        lines: [STONES], currencies: ["coins", "stones"],
        balances: { coins: 1695, gems: null, stones: null }, balanced: ["coins", "gems"],
      }));
      render(<LedgerPage />);

      await screen.findByText("Tier 2 Wave 50");
      // Once in the header, once on the row - and never as "0".
      expect(screen.getAllByText("not tracked")).toHaveLength(2);
      expect(screen.getByTitle("+10 stones")).toBeDefined();
    });

    it("keeps an unread coin balance apart from an untracked one", async () => {
      fetchLedger.mockResolvedValue(payload({
        lines: [STONES], currencies: ["stones"],
        balances: { coins: null, gems: null, stones: null }, balanced: ["coins", "gems"],
      }));
      render(<LedgerPage />);

      await screen.findByText("Tier 2 Wave 50");
      // Coins: header only, since no coin line is on screen. Stones: header
      // and row. Different words for a different reason to be blank.
      expect(screen.getAllByTitle("coins balance not read yet")).toHaveLength(1);
      expect(screen.getAllByTitle("the ledger keeps no running stones balance")).toHaveLength(2);
    });
  });

  group("claims", () => {
    it("can be filtered to - the kind was missing from the chips", async () => {
      fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE] }));
      render(<LedgerPage />);

      fireEvent.click(await screen.findByRole("button", { name: "mission claim" }));
      await waitFor(() =>
        expect(fetchLedger).toHaveBeenLastCalledWith({ kind: "MISSION_CLAIM" }),
      );
    });

    it("refresh the page when one lands, not only after a purchase", async () => {
      fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE] }));
      const { rerender } = render(<LedgerPage />);
      await screen.findByText("Health");
      const before = fetchLedger.mock.calls.length;

      stream.events = [{ type: "MissionClaimed", seq: 1 }];
      rerender(<LedgerPage />);

      await waitFor(() => expect(fetchLedger.mock.calls.length).toBe(before + 1));
    });

    it("do not trigger a refresh for events that write no ledger line", async () => {
      fetchLedger.mockResolvedValue(payload({ lines: [A_PURCHASE] }));
      const { rerender } = render(<LedgerPage />);
      await screen.findByText("Health");
      const before = fetchLedger.mock.calls.length;

      stream.events = [{ type: "Tapped", seq: 2 }];
      rerender(<LedgerPage />);

      await screen.findByText("Health");
      expect(fetchLedger.mock.calls.length).toBe(before);
    });
  });
});
