import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { EmulatorRecovery } from "./EmulatorRecovery";

const api = vi.hoisted(() => ({ fetchFleetInstances: vi.fn(), fetchHostStatus: vi.fn(),
  startFleetInstance: vi.fn() }));
vi.mock("@/lib/api", () => api);

beforeEach(() => {
  api.fetchFleetInstances.mockReset().mockResolvedValue({ can_start: true, instances: [
    { name: "Tiramisu64_6", endpoint: "127.0.0.1:5615", state: "running", template: true },
    { name: "Tiramisu64_18", endpoint: "127.0.0.1:5735", state: "stopped", template: false },
  ] });
  api.fetchHostStatus.mockReset().mockResolvedValue({ bot: {
    error: "identity incident: missing ADB endpoint 127.0.0.1:5555",
  } });
  api.startFleetInstance.mockReset().mockResolvedValue({ can_start: true, instances: [] });
});

test("shows the missing endpoint and starts only the chosen emulator", async () => {
  render(<EmulatorRecovery />);
  expect(await screen.findByText(/missing ADB endpoint 127.0.0.1:5555/)).toBeInTheDocument();
  expect(screen.getByText(/Unopened template · keep Tower closed/)).toBeInTheDocument();
  expect(api.startFleetInstance).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Start emulator" }));
  expect(api.startFleetInstance).toHaveBeenCalledWith("Tiramisu64_18");
});
