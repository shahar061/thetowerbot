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

test("single settings remains available without a selected account", () => {
  state.pathname = "/settings/";
  state.selected = null;
  render(<AccountShell><p>Telegram settings</p></AccountShell>);
  expect(screen.getByText("Telegram settings")).toBeInTheDocument();
  expect(screen.queryByText("No account selected")).not.toBeInTheDocument();
});

test("archived account shows history but blocks live content", () => {
  state.pathname = "/control/";
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: false, dashboard_url: null };
  render(<AccountShell><p>private data</p></AccountShell>);
  expect(screen.getByText("No live bot for this account")).toBeInTheDocument();
  expect(screen.queryByText("private data")).not.toBeInTheDocument();
});

test("the overview shows saved evidence for a stopped account", () => {
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: false, dashboard_url: null };
  render(<AccountShell><p>saved account overview</p></AccountShell>);
  expect(screen.getByText("saved account overview")).toBeInTheDocument();
  expect(screen.queryByText("No live bot for this account")).not.toBeInTheDocument();
  expect(screen.queryByTestId("runtime")).not.toBeInTheDocument();
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
  state.pathname = "/control/";
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: true, dashboard_url: "http://127.0.0.1:10018/" };
  render(<AccountShell><p>main process live data</p></AccountShell>);
  expect(screen.queryByText("main process live data")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open worker dashboard" })).toHaveAttribute(
    "href", "http://127.0.0.1:10018/");
  expect(screen.getByRole("link", { name: "Open worker Strategy" })).toHaveAttribute(
    "href", "http://127.0.0.1:10018/strategy/");
});

test("the remote worker overview renders without the host runtime gate", () => {
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: true, dashboard_url: "http://127.0.0.1:10018/" };
  render(<AccountShell><p>scoped worker overview</p></AccountShell>);
  expect(screen.getByText("scoped worker overview")).toBeInTheDocument();
  expect(screen.queryByTestId("runtime")).not.toBeInTheDocument();
});

test("the local running worker overview retains its runtime gate", () => {
  state.selected = { key: "worker:Air18", account_id: "ABC12345", instance: "Air18",
    kind: "worker", running: true, dashboard_url: `${window.location.origin}/` };
  render(<AccountShell><p>local account overview</p></AccountShell>);
  expect(screen.getByTestId("runtime")).toHaveTextContent("local account overview");
});

for (const pathname of ["/fleet/reroll", "/fleet/reroll/", "/fleet/reroll/strategies/", "/fleet/reroll/progression/", "/fleet/reroll/history/", "/fleet/reroll/settings/"]) {
  test(`reroll workspace ${pathname} has no global account picker or runtime gate`, () => {
    state.pathname = pathname;
    render(<AccountShell><p>fleet content</p></AccountShell>);
    expect(screen.queryByRole("combobox", { name: "Game account" })).not.toBeInTheDocument();
    expect(screen.getByText("fleet content")).toBeInTheDocument();
    expect(screen.queryByTestId("runtime")).not.toBeInTheDocument();
  });
}

test("similar route prefix retains the single-account shell", () => {
  state.pathname = "/fleet/rerolling/";
  render(<AccountShell><p>content</p></AccountShell>);
  expect(screen.getByRole("combobox", { name: "Game account" })).toBeInTheDocument();
});

test("the fleet state page gets the fleet shell, not the game account picker", () => {
  state.pathname = "/fleet/state/";
  render(<AccountShell><p>fleet columns</p></AccountShell>);
  expect(screen.getByText("fleet columns")).toBeInTheDocument();
  expect(screen.queryByLabelText("Game account")).not.toBeInTheDocument();
});
test('Cards is readable for an archived account',()=>{
 state.pathname='/cards/';state.selected={key:'worker:old',account_id:'old',instance:'old',kind:'worker',running:false,dashboard_url:null};
 render(<AccountShell><p>Archived Cards evidence</p></AccountShell>);
 expect(screen.getByText('Archived Cards evidence')).toBeInTheDocument();
 expect(screen.queryByText('No live bot for this account')).not.toBeInTheDocument();
});

test.each([true, false])("Workshop is readable for remote or stopped accounts: %s", running => {
 state.pathname = "/workshop/";
 state.selected = { key: "worker:A", account_id: "A", instance: "A", kind: "worker", running, dashboard_url: "http://127.0.0.1:10082/" };
 render(<AccountShell><p>Selected Workshop</p></AccountShell>);
 expect(screen.getByText("Selected Workshop")).toBeInTheDocument();
 expect(screen.queryByTestId("runtime")).not.toBeInTheDocument();
});
