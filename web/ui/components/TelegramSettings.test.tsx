import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { TelegramSettings } from "./TelegramSettings";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), save: vi.fn(), preview: vi.fn() }));
vi.mock("@/lib/api", () => ({
  fetchTelegramSettings: mocks.fetch,
  saveTelegramSettings: mocks.save,
  previewTelegramMessage: mocks.preview,
}));

const profile = { enabled: true, interval_minutes: 60,
  fields: ["screen", "scans", "wallet", "run", "runs_completed", "taps", "skips", "last_error"] };
const response = { mode: "single", profile, configured: true, token_present: true,
  chat_id_present: true, chat_id_masked: "*****4321", suppressed: false,
  interval_overridden: false, effective_interval_seconds: null };

beforeEach(() => {
  mocks.fetch.mockReset().mockResolvedValue(response);
  mocks.save.mockReset().mockImplementation(async (_mode, next) => ({ ...response, profile: next }));
  mocks.preview.mockReset().mockImplementation(async (_mode, next) => ({
    message: `The Tower bot - running${next.fields.includes("screen") ? "\nScreen: BATTLE" : ""}`,
  }));
});

test("preview changes before saving and Save persists the selected fields", async () => {
  render(<TelegramSettings mode="single" />);
  expect(await screen.findByText(/Screen: BATTLE/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("switch", { name: "Screen" }));
  await waitFor(() => expect(screen.queryByText(/Screen: BATTLE/)).not.toBeInTheDocument());
  expect(mocks.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith("single", expect.objectContaining({
    fields: expect.not.arrayContaining(["screen"]),
  })));
  expect(await screen.findByText(/Saved/)).toBeInTheDocument();
});

test("invalid interval blocks Save while Reset restores the saved value", async () => {
  render(<TelegramSettings mode="single" />);
  const input = await screen.findByRole("spinbutton", { name: "Update every" });
  fireEvent.change(input, { target: { value: "0" } });
  expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reset changes" }));
  expect(input).toHaveValue(60);
});

test("missing credentials are explained without exposing a token", async () => {
  mocks.fetch.mockResolvedValue({ ...response, configured: false, token_present: false });
  render(<TelegramSettings mode="single" />);
  expect(await screen.findByText(/TELEGRAM_BOT_TOKEN/)).toBeInTheDocument();
  expect(screen.queryByText(/VERY-SECRET/)).not.toBeInTheDocument();
});
