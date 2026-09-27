import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { RecoverySettingsPanel } from "./RecoverySettingsPanel";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), save: vi.fn() }));
vi.mock("@/lib/api", () => ({ fetchRecoverySettings: mocks.fetch, saveRecoverySettings: mocks.save }));

const settings = { mode: "off", model: "openai/gpt-5.4-nano", deadline_seconds: 15,
  max_calls: 2, max_actions: 3, cooldown_seconds: 600,
  incident_limit_microusd: 50_000, daily_limit_microusd: 1_000_000 };
const response = { settings, shadow_worker: null, settings_revision: 1, policy: {
  active: { incident_limit_microusd: 50_000, daily_limit_microusd: 1_000_000,
    max_calls: 2, revision: 1, disabled_request_id: null },
  requested: { incident_limit_microusd: 50_000, daily_limit_microusd: 1_000_000, max_calls: 2 },
  requested_policy_conflict: false }, assist_allowed_actions: [],
  status: { producer: "unknown", workers: [] } };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  mocks.fetch.mockReset().mockResolvedValue(response);
  mocks.save.mockReset().mockImplementation(async (draft) => ({ ...response, settings: draft,
    settings_revision: 2 }));
});

test("shows active caps, unknown worker status and backend-only credential instructions", async () => {
  render(<RecoverySettingsPanel />);
  expect(await screen.findByText(/Worker recovery status unknown/)).toBeInTheDocument();
  expect(screen.getByText(/CLAUDE_OPENROUTER_API_KEY/)).toBeInTheDocument();
  expect(screen.getByText(/\$1\.00/)).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /API key/i })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save recovery settings" })).toBeDisabled();
});

test("saves shadow with both reviewed revisions and never offers uncalibrated assist", async () => {
  render(<RecoverySettingsPanel />);
  const mode = await screen.findByRole("combobox", { name: "Recovery mode" });
  expect(screen.getByRole("option", { name: /Assist/ })).toBeDisabled();
  fireEvent.change(mode, { target: { value: "shadow" } });
  expect(screen.getByRole("button", { name: "Save recovery settings" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "Shadow worker ID" }), { target: { value: "worker-a" } });
  fireEvent.click(screen.getByRole("button", { name: "Save recovery settings" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({ mode: "shadow" }), "worker-a", 1, 1));
});

test("Live uses a compact read-only summary with unknown application", async () => {
  render(<RecoverySettingsPanel compact />);
  expect(await screen.findByText(/Worker application/)).toHaveTextContent("unknown");
  expect(screen.getByRole("link", { name: /Recovery settings/ })).toHaveAttribute("href", "/fleet/reroll/settings/");
  expect(screen.queryByRole("button", { name: "Save recovery settings" })).not.toBeInTheDocument();
});

test("USD budget inputs convert precisely and reject excess precision", async () => {
  render(<RecoverySettingsPanel />);
  const incident = await screen.findByRole("textbox", { name: "Incident cap (USD)" });
  expect(incident).toHaveValue("0.05");
  fireEvent.change(incident, { target: { value: "0.0000001" } });
  expect(screen.getByRole("button", { name: "Save recovery settings" })).toBeDisabled();
  fireEvent.change(incident, { target: { value: "0.04" } });
  fireEvent.click(screen.getByRole("button", { name: "Save recovery settings" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith(
    expect.objectContaining({ incident_limit_microusd: 40_000 }), null, 1, 1));
});

test("a delayed old reload cannot replace a newer successful save", async () => {
  render(<RecoverySettingsPanel />);
  const incident = await screen.findByRole("textbox", { name: "Incident cap (USD)" });
  fireEvent.change(incident, { target: { value: "0.04" } });
  const get = deferred<typeof response>();
  const put = deferred<typeof response>();
  mocks.fetch.mockReturnValueOnce(get.promise);
  mocks.save.mockReturnValueOnce(put.promise);
  fireEvent.click(screen.getByRole("button", { name: "Reload status" }));
  fireEvent.click(screen.getByRole("button", { name: "Save recovery settings" }));
  expect(mocks.save).toHaveBeenCalledOnce();
  const revised = { ...response, settings: { ...settings, incident_limit_microusd: 40_000 },
    settings_revision: 2, policy: { ...response.policy,
      active: { ...response.policy.active, incident_limit_microusd: 40_000, revision: 2 } } };
  await act(async () => put.resolve(revised));
  expect(screen.getByText(/Settings revision/)).toHaveTextContent("2");
  await act(async () => get.resolve(response));
  expect(incident).toHaveValue("0.04");
  expect(screen.getByText(/Settings revision/)).toHaveTextContent("2");
});

test("edits and reload are blocked while a save is pending", async () => {
  render(<RecoverySettingsPanel />);
  const incident = await screen.findByRole("textbox", { name: "Incident cap (USD)" });
  fireEvent.change(incident, { target: { value: "0.04" } });
  const put = deferred<typeof response>();
  mocks.save.mockReturnValueOnce(put.promise);
  fireEvent.click(screen.getByRole("button", { name: "Save recovery settings" }));
  expect(incident).toBeDisabled();
  expect(screen.getByRole("button", { name: "Reload status" })).toBeDisabled();
  fireEvent.change(incident, { target: { value: "0.03" } });
  expect(incident).toHaveValue("0.04");
  await act(async () => put.resolve({ ...response,
    settings: { ...settings, incident_limit_microusd: 40_000 }, settings_revision: 2 }));
  expect(incident).toHaveValue("0.04");
});

test("a failed save requires a fresh reviewed snapshot before another save", async () => {
  render(<RecoverySettingsPanel />);
  const incident = await screen.findByRole("textbox", { name: "Incident cap (USD)" });
  fireEvent.change(incident, { target: { value: "0.04" } });
  mocks.save.mockRejectedValueOnce(new Error("revision changed"));
  fireEvent.click(screen.getByRole("button", { name: "Save recovery settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Reload the active policy");
  expect(screen.getByRole("button", { name: "Save recovery settings" })).toBeDisabled();
  const newer = { ...response, settings_revision: 2,
    policy: { ...response.policy, active: { ...response.policy.active, revision: 2 } } };
  mocks.fetch.mockResolvedValueOnce(newer);
  fireEvent.click(screen.getByRole("button", { name: "Reload status" }));
  await waitFor(() => expect(screen.getByText(/Settings revision/)).toHaveTextContent("2"));
  expect(incident).toHaveValue("0.05");
});

test("a compact summary fetch failure is a status, never a page alert", async () => {
  mocks.fetch.mockReset().mockRejectedValue(new Error("offline"));
  render(<RecoverySettingsPanel compact />);
  expect(await screen.findByText(/Recovery status unavailable: offline/)).toHaveAttribute("role", "status");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
