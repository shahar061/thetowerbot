import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { makeAccount, makePayload } from "./fixtures";
import FleetStatePage from "./page";
import { SELECTION_KEY } from "./selection";

const fetchState = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchFleetState: fetchState }));

const live = makeAccount({ id: "Air_1", online: true, scan: 7, bot: { screen: "IN_RUN", now: null, live: true },
  battle: { tier: 3, wave: 120, cash: 10, elapsed_s: 60, best_wave: 200 }, balances: { coins: 1000, gems: 5, stones: null } });
const menu = makeAccount({ id: "Air_2", online: true, scan: 9, bot: { screen: "MAIN_MENU", now: null, live: false },
  balances: { coins: 500, gems: 1, stones: null } });
const columns = () => screen.queryAllByRole("article").map(article => article.getAttribute("aria-label"));

beforeEach(() => { fetchState.mockReset(); window.localStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); });

test("chips and Only live filter the columns, and the pick is remembered", async () => {
  fetchState.mockResolvedValue(makePayload([live, menu]));
  const view = render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_1", "Air_2"]));
  expect(screen.getByText("1.50K")).toBeInTheDocument();  // fleet coins
  expect(screen.getByText("#9")).toBeInTheDocument();      // heartbeat: newest scan

  fireEvent.click(screen.getByRole("button", { name: /Air_1/ }));
  expect(columns()).toEqual(["Air_2"]);
  expect(JSON.parse(window.localStorage.getItem(SELECTION_KEY)!)).toEqual({ mode: "custom", ids: ["Air_2"] });

  fireEvent.click(screen.getByRole("button", { name: "Only live" }));
  expect(columns()).toEqual(["Air_1"]);
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(columns()).toEqual(["Air_1", "Air_2"]);

  fireEvent.click(screen.getByRole("button", { name: /Air_1/ }));
  view.unmount();
  render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_2"]));
});

test("deselecting everything explains how to get the columns back", async () => {
  fetchState.mockResolvedValue(makePayload([live]));
  render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_1"]));
  fireEvent.click(screen.getByRole("button", { name: /Air_1/ }));
  expect(screen.getByText(/No emulators selected/)).toBeInTheDocument();
});

test("blocked storage still renders every emulator", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  fetchState.mockResolvedValue(makePayload([live, menu]));
  render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_1", "Air_2"]));
  fireEvent.click(screen.getByRole("button", { name: /Air_2/ }));
  expect(columns()).toEqual(["Air_1"]);
});

test("a failed first request says the state is unavailable and shows the pill", async () => {
  fetchState.mockRejectedValue(new Error("down"));
  render(<FleetStatePage />);
  await waitFor(() => expect(screen.getByText("connection lost")).toBeInTheDocument());
  expect(screen.getByText(/Fleet state unavailable/)).toBeInTheDocument();
});

test("Workshop switches between bars and table and remembers the view after remount", async () => {
  const category = { unlocked: 0, total: 0, skills: [], next_unlock: null };
  fetchState.mockResolvedValue(makePayload([makeAccount({ ...live, workshop: {
    totals: { attack: 0, defense: 0, utility: 0 }, recent: [],
    categories: { attack: category, defense: category, utility: category },
  } })]));
  const view = render(<FleetStatePage />);
  expect(await screen.findByRole("button", { name: "Bars" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("region", { name: "Attack workshop levels" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Table" }));
  expect(screen.queryByRole("region", { name: "Attack workshop levels" })).toBeNull();
  expect(screen.getByRole("region", { name: "Attack workshop" })).toBeInTheDocument();
  view.unmount();
  render(<FleetStatePage />);
  expect(await screen.findByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Bars" }));
  expect(screen.getByRole("region", { name: "Attack workshop levels" })).toBeInTheDocument();
});
