import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import FleetPage from "./page";
import { fetchFleet, fetchFleetPreview, fetchFleetSetup, saveFleetSetup, startFleetSource, requestFleetProvision } from "@/lib/api";

vi.mock("@/lib/api", () => ({ fetchFleet: vi.fn(), fetchFleetPreview: vi.fn(), fetchFleetSetup: vi.fn(), saveFleetSetup: vi.fn(), startFleetSource: vi.fn(), requestFleetProvision: vi.fn(), resolveFleetTarget: vi.fn(), resumeFleetFirstLaunch: vi.fn() }));

const capacity = { limit: 5, used: 1, available: 4 };

beforeEach(() => {
  vi.mocked(fetchFleet).mockReset();
  vi.mocked(fetchFleetPreview).mockReset();
  vi.mocked(fetchFleetSetup).mockReset();
  vi.mocked(requestFleetProvision).mockReset();
  vi.mocked(saveFleetSetup).mockReset();
  vi.mocked(startFleetSource).mockReset();
  vi.mocked(fetchFleetSetup).mockResolvedValue({ configured: false, settings: null, qualifications: [], host: { installed_prefix: "seed_", instance_count: 1 } });
});

test("Fleet polling waits for the previous host check to finish", async () => {
  vi.useFakeTimers();
  try {
    let resolveFleet!: (value: { capacity: typeof capacity; sources: []; jobs: [] }) => void;
    vi.mocked(fetchFleet).mockImplementationOnce(() => new Promise(resolve => { resolveFleet = resolve; }));
    vi.mocked(fetchFleet).mockResolvedValue({ capacity, sources: [], jobs: [] });
    render(<FleetPage />);
    expect(fetchFleet).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(12_000); });
    expect(fetchFleet).toHaveBeenCalledTimes(1);
    await act(async () => { resolveFleet({ capacity, sources: [], jobs: [] }); });
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(fetchFleet).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

test("unchanged Fleet polls do not cancel a slow clone preview", async () => {
  vi.useFakeTimers();
  try {
    const snapshot = { capacity, sources: [{ instance: "seed", state: "parallel_session_qualified" as const,
      reason: "qualified" }], jobs: [] };
    vi.mocked(fetchFleet).mockImplementation(async () => ({ ...snapshot, sources: [...snapshot.sources] }));
    vi.mocked(fetchFleetSetup).mockResolvedValue({ configured: true,
      settings: { capacity: 5, name_prefix: "seed_", qualification_id: "m05" },
      qualifications: [{ id: "m05", source_instance: "seed" }],
      host: { installed_prefix: "seed_", instance_count: 1 } });
    const previewResolvers: Array<(value: { mode: "clone"; source: string; count: number;
      targets: string[]; state: "eligible" }) => void> = [];
    vi.mocked(fetchFleetPreview).mockImplementation(() => new Promise(resolve => { previewResolvers.push(resolve); }));
    render(<FleetPage />);
    await act(async () => {});
    expect(fetchFleetPreview).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("checkbox", { name: /confirm cloning/i })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(fetchFleet).toHaveBeenCalledTimes(2);
    expect(fetchFleetPreview).toHaveBeenCalledTimes(1);
    await act(async () => { previewResolvers[0]({ mode: "clone", source: "seed", count: 1,
      targets: ["seed_1"], state: "eligible" }); });
    expect(screen.getByText("seed_1")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /confirm cloning/i })).not.toBeDisabled();
  } finally {
    vi.useRealTimers();
  }
});

test("setup saves the selected live source and starts its emulator", async () => {
  const initial = { configured: false, settings: null, qualifications: [{ id: "m05-air6", source_instance: "Tiramisu64_6" }], host: { installed_prefix: "Tiramisu64_", instance_count: 5 } };
  vi.mocked(fetchFleetSetup).mockResolvedValue(initial);
  vi.mocked(fetchFleet).mockResolvedValue({ capacity: { limit: 7, used: 5, available: 2 }, sources: [], jobs: [] });
  vi.mocked(fetchFleetPreview).mockResolvedValue({ mode: "clone", source: "Tiramisu64_6", count: 1, targets: [], state: "blocked" });
  vi.mocked(saveFleetSetup).mockResolvedValue({ ...initial, configured: true, settings: { capacity: 7, name_prefix: "Tiramisu64_", qualification_id: "m05-air6" } });
  vi.mocked(startFleetSource).mockResolvedValue({ capacity: { limit: 7, used: 5, available: 2 }, sources: [{ instance: "Tiramisu64_6", state: "parallel_session_qualified", reason: "qualified" }], jobs: [] });
  render(<FleetPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Save Fleet setup" }));
  await waitFor(() => expect(saveFleetSetup).toHaveBeenCalledWith({ capacity: 7, name_prefix: "Tiramisu64_", qualification_id: "m05-air6" }));
  fireEvent.click(await screen.findByRole("button", { name: "Start and verify template" }));
  await waitFor(() => expect(startFleetSource).toHaveBeenCalled());
});

test("blocked source is visible and cannot submit a clone request", async () => {
  vi.mocked(fetchFleet).mockResolvedValue({ capacity, sources: [{ instance: "seed", state: "blocked", reason: "qualification expired" }], jobs: [] });
  vi.mocked(fetchFleetPreview).mockResolvedValue({ mode: "clone", source: "", count: 1, targets: [], state: "blocked", reason: "qualification expired" });
  render(<FleetPage />);
  fireEvent.click(screen.getByRole("radio", { name: /clone/i }));
  expect(await screen.findByText(/qualification expired/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /request clones/i })).toBeDisabled();
});

test("qualified source requires explicit confirmation and submits bounded count", async () => {
  vi.mocked(fetchFleet).mockResolvedValue({ capacity, sources: [{ instance: "seed", state: "parallel_session_qualified", reason: "qualified" }], jobs: [] });
  vi.mocked(fetchFleetPreview).mockResolvedValue({ mode: "clone", source: "seed", count: 1, targets: ["seed_1"], state: "eligible" });
  vi.mocked(requestFleetProvision).mockResolvedValue({ id: "job", mode: "clone", source: "seed", requested_at: 1, clones: [{ state: "queued", reason: "awaiting_staging" }] });
  render(<FleetPage />);
  fireEvent.click(screen.getByRole("radio", { name: /clone/i }));
  await screen.findByRole("option", { name: "seed" });
  expect(await screen.findByText("seed_1")).toBeInTheDocument();
  const button = screen.getByRole("button", { name: /request clones/i });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /confirm/i }));
  fireEvent.click(button);
  await waitFor(() => expect(requestFleetProvision).toHaveBeenCalledWith({ mode: "clone", source: "seed", count: 1, targets: ["seed_1"], state: "eligible" }));
});

test("fresh mode shows the exact target before confirmation", async () => {
  vi.mocked(fetchFleet).mockResolvedValue({ capacity, sources: [], jobs: [] });
  vi.mocked(fetchFleetPreview).mockResolvedValue({ mode: "fresh", source: null, count: 1, targets: ["seed_1"], state: "eligible" });
  render(<FleetPage />);
  fireEvent.click(screen.getByRole("radio", { name: /fresh/i }));
  expect(await screen.findByText("seed_1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /request fresh/i })).toBeDisabled();
});

test("reroll entry point explains and previews one clone before creating it", async () => {
  vi.mocked(fetchFleetSetup).mockResolvedValue({
    configured: true, settings: { capacity: 7, name_prefix: "Tiramisu64_", qualification_id: "m05-air6" },
    qualifications: [{ id: "m05-air6", source_instance: "Tiramisu64_6" }],
    host: { installed_prefix: "Tiramisu64_", instance_count: 5 },
  });
  vi.mocked(fetchFleet).mockResolvedValue({
    capacity: { limit: 7, used: 5, available: 2 },
    sources: [{ instance: "Tiramisu64_6", state: "parallel_session_qualified", reason: "qualified" }], jobs: [],
  });
  vi.mocked(fetchFleetPreview).mockResolvedValue({
    mode: "clone", source: "Tiramisu64_6", count: 1,
    targets: ["Tiramisu64_18"], state: "eligible",
  });
  vi.mocked(requestFleetProvision).mockResolvedValue({
    id: "reroll-job", mode: "clone", source: "Tiramisu64_6", requested_at: 1,
    clones: [{ instance: "Tiramisu64_18", state: "queued", reason: "awaiting_staging" }],
  });
  render(<FleetPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Start a reroll" }));
  expect(await screen.findByText(/new Tower account in a separate clone/i)).toBeInTheDocument();
  expect(requestFleetProvision).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: /Create Tiramisu64_18/ }));
  await waitFor(() => expect(requestFleetProvision).toHaveBeenCalledWith({
    mode: "clone", source: "Tiramisu64_6", count: 1,
    targets: ["Tiramisu64_18"], state: "eligible",
  }));
});

test("accepted reroll leaves Creating state before the next slow Fleet refresh", async () => {
  const snapshot = { capacity, sources: [{ instance: "seed", state: "parallel_session_qualified" as const,
    reason: "qualified" }], jobs: [] };
  vi.mocked(fetchFleet).mockResolvedValueOnce(snapshot);
  vi.mocked(fetchFleet).mockImplementationOnce(() => new Promise(() => {}));
  vi.mocked(fetchFleetPreview).mockResolvedValue({ mode: "clone", source: "seed", count: 1,
    targets: ["seed_1"], state: "eligible" });
  vi.mocked(requestFleetProvision).mockResolvedValue({ id: "job", mode: "clone", source: "seed",
    requested_at: 1, clones: [{ instance: "seed_1", state: "queued", reason: "awaiting_staging" }] });
  render(<FleetPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Start a reroll" }));
  fireEvent.click(await screen.findByRole("button", { name: "Create seed_1" }));
  expect(await screen.findByText(/Reroll request recorded/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Request clones" })).toBeInTheDocument();
});

test("empty Fleet points to the reroll action without creating an instance", async () => {
  vi.mocked(fetchFleet).mockResolvedValue({ capacity, sources: [], jobs: [] });
  vi.mocked(fetchFleetPreview).mockResolvedValue({
    mode: "clone", source: null, count: 1, targets: [], state: "blocked",
    reason: "live_qualification_missing_stale_or_changed",
  });
  render(<FleetPage />);
  expect(await screen.findByText(/No reroll accounts yet/i)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /how rerolls work/i })).toHaveAttribute("href", "/fleet/how-it-works/");
  expect(requestFleetProvision).not.toHaveBeenCalled();
});
