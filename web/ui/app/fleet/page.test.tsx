import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import FleetPage from "./page";
import { fetchFleet, requestFleetClones } from "@/lib/api";

vi.mock("@/lib/api", () => ({ fetchFleet: vi.fn(), requestFleetClones: vi.fn() }));

beforeEach(() => {
  vi.mocked(fetchFleet).mockReset();
  vi.mocked(requestFleetClones).mockReset();
});

test("blocked source is visible and cannot submit a clone request", async () => {
  vi.mocked(fetchFleet).mockResolvedValue({ sources: [{ instance: "seed", state: "blocked", reason: "qualification expired" }], jobs: [] });
  render(<FleetPage />);
  expect(await screen.findByText(/qualification expired/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /request clones/i })).toBeDisabled();
});

test("qualified source requires explicit confirmation and submits bounded count", async () => {
  vi.mocked(fetchFleet).mockResolvedValue({ sources: [{ instance: "seed", state: "qualified", reason: "qualified" }], jobs: [] });
  vi.mocked(requestFleetClones).mockResolvedValue({ id: "job", source: "seed", requested_at: 1, clones: [{ state: "queued", reason: "awaiting_staging" }] });
  render(<FleetPage />);
  await screen.findByRole("option", { name: "seed" });
  const button = screen.getByRole("button", { name: /request clones/i });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /confirm/i }));
  fireEvent.click(button);
  await waitFor(() => expect(requestFleetClones).toHaveBeenCalledWith("seed", 1));
});
