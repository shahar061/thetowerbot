import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { FleetTimingSettings } from "./FleetTimingSettings";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), save: vi.fn() }));
vi.mock("@/lib/api", () => ({ fetchFleetTiming: mocks.fetch, saveFleetTiming: mocks.save }));

beforeEach(() => {
  mocks.fetch.mockReset().mockResolvedValue({ menu_interval: 2 });
  mocks.save.mockReset().mockImplementation(async (timing) => ({
    ...timing, workers_updated: 2, workers_not_running: 1,
  }));
});

test("committing a new pace saves it and reports which workers took it live", async () => {
  render(<FleetTimingSettings />);
  const field = await screen.findByLabelText("Scan interval, outside battle (s)");
  expect(field).toHaveValue(2);
  fireEvent.change(field, { target: { value: "0.7" } });
  fireEvent.blur(field);
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith({ menu_interval: 0.7 }));
  expect(await screen.findByText(/Applied live to 2 running worker\(s\); 1 will pick it up/))
    .toBeInTheDocument();
});

test("an unchanged value does not save", async () => {
  render(<FleetTimingSettings />);
  const field = await screen.findByLabelText("Scan interval, outside battle (s)");
  fireEvent.blur(field);
  expect(mocks.save).not.toHaveBeenCalled();
});

test("a failed save shows the error", async () => {
  mocks.save.mockRejectedValue(new Error("menu_interval: too large"));
  render(<FleetTimingSettings />);
  const field = await screen.findByLabelText("Scan interval, outside battle (s)");
  fireEvent.change(field, { target: { value: "5000" } });
  fireEvent.blur(field);
  expect(await screen.findByRole("alert")).toHaveTextContent("too large");
});
