import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import FleetPage from "./page";
import { fetchFleet, fetchFleetPreview, requestFleetProvision } from "@/lib/api";

vi.mock("@/lib/api", () => ({ fetchFleet: vi.fn(), fetchFleetPreview: vi.fn(), requestFleetProvision: vi.fn(), resolveFleetTarget: vi.fn() }));

const capacity = { limit: 5, used: 1, available: 4 };

beforeEach(() => {
  vi.mocked(fetchFleet).mockReset();
  vi.mocked(fetchFleetPreview).mockReset();
  vi.mocked(requestFleetProvision).mockReset();
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
  expect(await screen.findByText("seed_1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /request fresh/i })).toBeDisabled();
});
