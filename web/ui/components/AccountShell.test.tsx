import { render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { AccountShell } from "./AccountShell";

const state = vi.hoisted(() => ({ pathname: "/", selected: null as null | {
  key: string; account_id: string | null; instance: string | null;
  kind: "worker" | "unattributed"; running: boolean; dashboard_url: string | null;
} }));
vi.mock("next/navigation", () => ({ usePathname: () => state.pathname }));
vi.mock("next/link", () => ({ default: ({ children, href }: { children: React.ReactNode; href: string }) =>
  <a href={href}>{children}</a> }));
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({
  accounts: state.selected ? [state.selected] : [], selected: state.selected,
  loading: false, error: null, choose: vi.fn(),
}) }));
vi.mock("./RuntimeGate", () => ({ RuntimeGate: ({ children }: { children: React.ReactNode }) =>
  <div data-testid="runtime">{children}</div> }));
vi.mock("./EmulatorRecovery", () => ({ EmulatorRecovery: () => <p>Emulator choices</p> }));

beforeEach(() => { state.pathname = "/"; state.selected = null; });

test("empty dashboard points to reroll without showing old live data", () => {
  render(<AccountShell><p>old live data</p></AccountShell>);
  expect(screen.getByText("No account selected")).toBeInTheDocument();
  expect(screen.queryByText("old live data")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Start a reroll" })).toHaveAttribute("href", "/fleet/reroll/");
});

test("archived account shows history but blocks live content", () => {
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: false, dashboard_url: null };
  render(<AccountShell><p>private data</p></AccountShell>);
  expect(screen.getByText("No live bot for this account")).toBeInTheDocument();
  expect(screen.queryByText("private data")).not.toBeInTheDocument();
});

test("archived history renders under account selector", () => {
  state.pathname = "/runs/";
  state.selected = { key: "unattributed", account_id: null, instance: null,
    kind: "unattributed", running: false, dashboard_url: null };
  render(<AccountShell><p>scoped runs</p></AccountShell>);
  expect(screen.getByText("scoped runs")).toBeInTheDocument();
  expect(screen.queryByTestId("runtime")).not.toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Unattributed history" })).toBeInTheDocument();
});

test("a running account on another worker opens its own dashboard", () => {
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: true, dashboard_url: "http://127.0.0.1:10018/" };
  render(<AccountShell><p>main process live data</p></AccountShell>);
  expect(screen.queryByText("main process live data")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open worker dashboard" })).toHaveAttribute(
    "href", "http://127.0.0.1:10018/");
  expect(screen.getByRole("link", { name: "Open worker Strategy" })).toHaveAttribute(
    "href", "http://127.0.0.1:10018/strategy/");
});
