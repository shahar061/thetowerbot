import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
