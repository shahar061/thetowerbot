import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { Sidebar } from "./Sidebar";
const state = vi.hoisted(() => ({ pathname: "/", errors: vi.fn(), strategies: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => state.pathname }));
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ selected: { key: "worker:A", running: true } }) }));
vi.mock("@/lib/api", () => ({ fetchErrors: state.errors, fetchStrategies: state.strategies }));
vi.mock("@/lib/useEventStream", () => ({ useConnected: () => true }));
vi.mock("@/components/ThemeToggle", () => ({ ThemeToggle: () => <button>Theme</button> }));
beforeEach(() => { state.pathname = "/"; vi.clearAllMocks(); state.errors.mockResolvedValue([]); state.strategies.mockResolvedValue({ active: "single-strategy" }); });

test.each(["/fleet/reroll/", "/fleet/reroll/strategies/", "/fleet/reroll/progression/", "/fleet/reroll/history/", "/fleet/reroll/stats/", "/fleet/reroll/ledger/", "/fleet/reroll/labs/"])("fleet navigation at %s excludes account signals and polling", pathname => {
  state.pathname = pathname;
  render(<Sidebar />);
  expect(screen.getByRole("link", { name: "Fleet Live" })).toHaveAttribute("href", "/fleet/reroll/");
  expect(screen.getByRole("link", { name: "Single emulator" })).toHaveAttribute("href", "/");
  expect(screen.getByRole("link", { name: "Strategy Studio" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Progression" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Workshop" })).toHaveAttribute("href", "/fleet/reroll/workshop/");
  expect(screen.getByRole("link", { name: "History" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Labs & Gems" })).toHaveAttribute("href", "/fleet/reroll/labs/");
  expect(screen.getByRole("link", { name: "Guide" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Control" })).not.toBeInTheDocument();
  expect(screen.queryByText("no bot")).not.toBeInTheDocument();
  expect(screen.queryByText("live")).not.toBeInTheDocument();
  expect(state.errors).not.toHaveBeenCalled();
  expect(state.strategies).not.toHaveBeenCalled();
  const current = screen.getAllByRole("link").filter(link => link.getAttribute("aria-current") === "page");
  expect(current).toHaveLength(1);
  expect(current[0]).toHaveAttribute("href", pathname);
});

test("single mode keeps its tools and clears account strategy on entering fleet", async () => {
  const view = render(<Sidebar />);
  expect(screen.getByRole("link", { name: "Control" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("single-strategy")).toBeInTheDocument());
  expect(state.strategies).toHaveBeenCalledTimes(1);
  state.pathname = "/fleet/reroll/history/";
  view.rerender(<Sidebar />);
  expect(screen.queryByText("single-strategy")).not.toBeInTheDocument();
});

test.each([
  ["/settings/", "/settings/", "Live"],
  ["/fleet/reroll/settings/", "/fleet/reroll/settings/", "Fleet Live"],
])("settings at %s stays in its workspace", async (pathname, href, homeLabel) => {
  state.pathname = pathname;
  render(<Sidebar />);
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", href);
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", { name: homeLabel })).toBeInTheDocument();
  if (pathname === "/settings/") {
    await waitFor(() => expect(screen.getByText("single-strategy")).toBeInTheDocument());
  }
});
