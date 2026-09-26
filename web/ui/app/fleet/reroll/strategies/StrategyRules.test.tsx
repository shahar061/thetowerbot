import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { DEFAULT_RULES, type LabsRow } from "@/lib/labs";
import { StrategyRules } from "./StrategyRules";

const row = { worker: "Air_38", account_id: "a", strategy_name: "x", read_at: 1, wallet: { coins: 1000, gems: 60 }, state: "ok",
  reason: null, recent: [], plan: { wallet_coins: 1000, jar: 200, slots: [{ slot: 1,
    now: { state: "idle", level: 2, completes_at: null, overdue_seconds: null, read_at: 1, stale: false },
    next: { lab_id: "labs.game-speed", name: "Game Speed", level: 2, price: 2500, seconds: 9000 },
    covered: false, automated: true, why: [], note: null }],
    gems: { wallet: 60, next: null, price: null, have: 60, need: null, automated: false, why: [], steps: [] } } } as LabsRow;

test("four sections, each rule tagged Live or Planned, safety rules locked", () => {
  render(<StrategyRules rules={DEFAULT_RULES} locked={false} rows={[]} onChange={vi.fn()} />);
  for (const name of ["Coins: Workshop vs Labs", "Labs", "Gems", "Fixed safety rules"])
    expect(screen.getByRole("group", { name })).toBeInTheDocument();
  expect(within(screen.getByRole("group", { name: "Coins: Workshop vs Labs" })).getAllByText("Live").length).toBe(2);
  expect(within(screen.getByRole("group", { name: "Labs" })).getAllByText("Planned").length).toBeGreaterThan(0);
  expect(screen.getByRole("group", { name: "Fixed safety rules" })).toHaveTextContent("Never rush a lab with gems");
});

test("changing the sharing mode reports new rules and keeps the rest", () => {
  const onChange = vi.fn();
  render(<StrategyRules rules={DEFAULT_RULES} locked={false} rows={[]} onChange={onChange} />);
  fireEvent.change(screen.getByLabelText("Lab share mode"), { target: { value: "save_pct" } });
  expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_RULES, coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "save_pct", pct: 25 } } });
  fireEvent.click(screen.getByLabelText("Start labs automatically"));
  expect(onChange.mock.calls.at(-1)![0].labs.auto_start).toBe(false);
});

test("a template's rules are read-only", () => {
  render(<StrategyRules rules={DEFAULT_RULES} locked rows={[]} onChange={vi.fn()} />);
  expect(screen.getByLabelText("Lab share mode")).toBeDisabled();
  expect(screen.getByLabelText("Keep gems")).toBeDisabled();
});

test("previews the wallet split for the selected emulator", () => {
  const saving = { ...DEFAULT_RULES, coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "save_pct" as const, pct: 20 } } };
  const { rerender } = render(<StrategyRules rules={saving} locked={false} rows={[row]} onChange={vi.fn()} />);
  const preview = screen.getByRole("complementary", { name: "Wallet split preview" });
  expect(preview).toHaveTextContent("Lab jar 200 / 2500 coins");
  expect(preview).toHaveTextContent("Workshop may spend 800 coins");
  const first = { ...DEFAULT_RULES, coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "labs_first" as const, pct: 25 } } };
  rerender(<StrategyRules rules={first} locked={false} rows={[row]} onChange={vi.fn()} />);
  expect(screen.getByRole("complementary", { name: "Wallet split preview" })).toHaveTextContent("Workshop paused until Game Speed starts");
});
