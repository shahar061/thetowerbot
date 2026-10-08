import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { LabSavingsSettings } from "@/lib/singleAccount";
import { LabSavings } from "./LabSavings";

const api = vi.hoisted(() => ({ fetch: vi.fn(), save: vi.fn() }));
vi.mock("@/lib/api", () => ({ fetchLabSavings: api.fetch, saveLabSavings: api.save }));

const settings: LabSavingsSettings = {
  account_id: "account-a", worker: "tower-a", revision: 7, share_mode: "save_pct",
  share_pct: 25, auto_start: true, editable: true, reason: null,
};
const split = { wallet: 1000, jar: 200, jar_target: { lab_id: "speed", name: "Lab Speed", level: 5, price: 800 },
  share_mode: "save_pct", share_pct: 25, workshop_limit_pct: 100, workshop_budget: 800 };

beforeEach(() => {
  api.fetch.mockReset().mockResolvedValue(settings);
  api.save.mockReset().mockImplementation(async (input) => ({ ...settings, ...input, revision: 8 }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

test("saving is explicit and submits the loaded account and revision with the chosen percentage", async () => {
  const saved = vi.fn();
  render(<LabSavings accountKey="worker:tower-a" expectedAccountId="account-a" split={split} onSaved={saved} />);
  const input = await screen.findByRole("spinbutton", { name: "Income saved for labs (%)" });
  fireEvent.change(input, { target: { value: "50" } });
  expect(api.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save lab settings" }));
  expect(await screen.findByText(/Lab settings saved/)).toBeInTheDocument();
  expect(api.save).toHaveBeenCalledWith({ expected_account_id: "account-a", expected_revision: 7, share_mode: "save_pct", share_pct: 50 });
  expect(saved).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Save lab settings" })).toBeDisabled();
});

test("invalid percentages cannot be saved and cancel restores the last settings", async () => {
  render(<LabSavings accountKey="worker:tower-a" split={split} onSaved={vi.fn()} />);
  const input = await screen.findByRole("spinbutton", { name: "Income saved for labs (%)" });
  fireEvent.change(input, { target: { value: "91" } });
  expect(screen.getByRole("button", { name: "Save lab settings" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
  expect(input).toHaveValue(25);
});

test("failed saves retain the draft and a revision conflict can reload current settings", async () => {
  api.save.mockRejectedValue(Object.assign(new Error("Settings changed in another session."), { status: 409 }));
  render(<LabSavings accountKey="worker:tower-a" split={split} onSaved={vi.fn()} />);
  const input = await screen.findByRole("spinbutton", { name: "Income saved for labs (%)" });
  fireEvent.change(input, { target: { value: "50" } });
  fireEvent.click(screen.getByRole("button", { name: "Save lab settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Settings changed in another session.");
  expect(input).toHaveValue(50);
  api.fetch.mockResolvedValue({ ...settings, revision: 8, share_pct: 40 });
  fireEvent.click(screen.getByRole("button", { name: "Reload current settings" }));
  await waitFor(() => expect(input).toHaveValue(40));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("background refresh preserves edits and never replaces their expected revision", async () => {
  vi.useFakeTimers();
  render(<LabSavings accountKey="worker:tower-a" split={split} onSaved={vi.fn()} />);
  await act(async () => { await Promise.resolve(); });
  const input = screen.getByRole("spinbutton", { name: "Income saved for labs (%)" });
  fireEvent.change(input, { target: { value: "50" } });
  api.fetch.mockResolvedValue({ ...settings, revision: 8, share_pct: 40 });
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(input).toHaveValue(50);
  expect(screen.getByRole("button", { name: "Save lab settings" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
  expect(input).toHaveValue(40);
});

test("a switched account cannot receive the previous account's pending save result", async () => {
  let resolveSave!: (value: LabSavingsSettings) => void;
  api.save.mockImplementation(() => new Promise<LabSavingsSettings>(resolve => { resolveSave = resolve; }));
  const saved = vi.fn();
  const view = render(<LabSavings accountKey="worker:tower-a" expectedAccountId="account-a" split={split} onSaved={saved} />);
  fireEvent.change(await screen.findByRole("spinbutton", { name: "Income saved for labs (%)" }), { target: { value: "50" } });
  fireEvent.click(screen.getByRole("button", { name: "Save lab settings" }));
  api.fetch.mockResolvedValue({ ...settings, account_id: "account-b", worker: "tower-b", share_pct: 10 });
  view.rerender(<LabSavings accountKey="worker:tower-b" expectedAccountId="account-b" split={null} onSaved={saved} />);
  await waitFor(() => expect(screen.getByRole("spinbutton", { name: "Income saved for labs (%)" })).toHaveValue(10));
  await act(async () => { resolveSave({ ...settings, share_pct: 50, revision: 8 }); });
  expect(screen.getByRole("spinbutton", { name: "Income saved for labs (%)" })).toHaveValue(10);
  expect(saved).not.toHaveBeenCalled();
  expect(screen.queryByText(/Lab settings saved/)).not.toBeInTheDocument();
});

test("read-only settings and account mismatches cannot be edited", async () => {
  api.fetch.mockResolvedValue({ ...settings, editable: false, reason: "No configured worker for this account." });
  const view = render(<LabSavings accountKey="worker:tower-a" expectedAccountId="account-a" split={null} onSaved={vi.fn()} />);
  expect(await screen.findByText("No configured worker for this account.")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Lab savings mode" })).toBeDisabled();
  api.fetch.mockResolvedValue(settings);
  view.rerender(<LabSavings accountKey="worker:tower-b" expectedAccountId="account-b" split={null} onSaved={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("different account");
  expect(screen.getByRole("button", { name: "Save lab settings" })).toBeDisabled();
});

test("a save response from another account requires reload before any retry", async () => {
  api.save.mockResolvedValue({ ...settings, account_id: "account-b", share_pct: 50, revision: 8 });
  const saved = vi.fn();
  render(<LabSavings accountKey="worker:tower-a" expectedAccountId="account-a" split={split} onSaved={saved} />);
  fireEvent.change(await screen.findByRole("spinbutton", { name: "Income saved for labs (%)" }), { target: { value: "50" } });
  fireEvent.click(screen.getByRole("button", { name: "Save lab settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("different account");
  expect(screen.getByRole("button", { name: "Save lab settings" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Reload current settings" })).toBeEnabled();
  expect(saved).not.toHaveBeenCalled();
});

test("historical accounts with unavailable settings show their reason without an editable mode or invented percentage", async () => {
  api.fetch.mockResolvedValue({ ...settings, revision: null, share_mode: null, share_pct: null,
    auto_start: false, editable: false, reason: "This historical account has no configured worker." });
  render(<LabSavings accountKey="history:account-a" expectedAccountId="account-a" split={null} onSaved={vi.fn()} />);
  expect(await screen.findByText("This historical account has no configured worker.")).toBeInTheDocument();
  expect(screen.getByText("Unavailable")).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Lab savings mode" })).not.toBeInTheDocument();
  expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save lab settings" })).not.toBeInTheDocument();
  expect(screen.queryByText(/ranked lab list determines/)).not.toBeInTheDocument();
  expect(api.save).not.toHaveBeenCalled();
});
