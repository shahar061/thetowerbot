import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StrategyPage from "./page";
import type { AdvisorSnapshot, AutopilotPreset, Strategy, Upgrade } from "@/lib/types";

const strategy = {
  name: "default",
  actions: [
    { name: "Damage", template: "d.png", enabled: true, threshold: 0.9, brightness_ratio: 0.75 },
  ],
  affordability: "digits",
  interval: 2,
  click_cooldown: 1,
  auto_navigate: false,
  max_runs: null,
  navigation_cooldown: 3,
  screen_confirmations: 2,
  shopping: {
    enabled: false,
    armed: false,
    visit_every_n_runs: 1,
    max_taps_per_visit: 40,
    workshop: [],
    cards: { enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" },
  },
};

const api = vi.hoisted(() => ({
  fetchStrategies: vi.fn(),
  fetchStrategy: vi.fn(),
  saveStrategy: vi.fn(),
  activateStrategy: vi.fn(),
  deleteStrategy: vi.fn(),
  fetchControl: vi.fn(),
  fetchUpgrades: vi.fn(() => Promise.resolve([] as Upgrade[])),
  fetchAutopilotPresets: vi.fn(() => Promise.resolve([] as AutopilotPreset[])),
  fetchAutopilot: vi.fn(() => Promise.resolve(null)),
  fetchAdvisor: vi.fn((profile: string) => Promise.resolve({ profile, import_id: null, imported_at: null, source: null, stale: false, missing_inputs: [], recommendations: [] } as AdvisorSnapshot)),
  importAdvisor: vi.fn(),
  stageAdvisor: vi.fn(),
}));
vi.mock("@/lib/api", () => api);

const sync = vi.hoisted(() => ({ onChange: null as null | (() => void) }));
vi.mock("@/lib/useControlSync", () => ({
  useControlSync: (cb: () => void) => {
    sync.onChange = cb;
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  sync.onChange = null;
  api.fetchStrategies.mockResolvedValue({ active: "default", names: ["default", "crit"] });
  api.fetchStrategy.mockResolvedValue(strategy);
  api.fetchUpgrades.mockResolvedValue([]);
  api.fetchAutopilotPresets.mockResolvedValue([]);
  api.fetchControl.mockResolvedValue({
    paused: false, strategy, affordability_available: ["digits", "brightness"],
  });
  api.saveStrategy.mockImplementation((_n: string, body: unknown) => Promise.resolve(body));
  // Defaults so a test that does not care about the dialogs never blocks on
  // one; the tests that do care override them.
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "prompt").mockReturnValue("copy");
});

const interval = () => screen.getByLabelText("Scan interval, in battle (s)") as HTMLInputElement;
const selector = () => screen.getByLabelText("Strategy") as HTMLSelectElement;

describe("StrategyPage", () => {
  it("keeps advisor recommendations in the shared Save/Revert draft flow", async () => {
    const now = Date.now() / 1000;
    api.fetchAdvisor.mockResolvedValueOnce({ profile: "default", import_id: "one", imported_at: now, source: { name: "Normalized export", version: "1", account_name: "Tower", exported_at: now, account_snapshot_at: now }, missing_inputs: [], stale: false, recommendations: [{ id: "health", path: "health", system: "workshop", upgrade: "Health", upgrade_id: "health", current_value: 10, target_value: 20, value_kind: "stat", cost: 100, currency: "coins", benefit: 2, can_stage: true, blocked_reason: null }] });
    api.stageAdvisor.mockImplementation(({ draft: working }: { draft: Strategy }) => Promise.resolve({ added: true, message: "Added Health to draft", draft: { ...working, shopping: { ...working.shopping, workshop: [{ name: "Health", category: "DEFENSE", enabled: true, target: 20 }] } } }));
    render(<StrategyPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Health to Workshop draft" }));
    await screen.findByText("Added Health to draft");
    expect(screen.getByText("Save")).not.toBeDisabled();
    fireEvent.click(screen.getByText("Revert"));
    expect(screen.getByText("Save")).toBeDisabled();
    expect(api.saveStrategy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Add Health to Workshop draft" }));
    await waitFor(() => expect(screen.getByText("Save")).not.toBeDisabled());
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(api.saveStrategy).toHaveBeenCalledWith("default", expect.objectContaining({ shopping: { ...strategy.shopping, workshop: [{ name: "Health", category: "DEFENSE", enabled: true, target: 20 }] } })));
  });
  it("loads the active profile on arrival", async () => {
    render(<StrategyPage />);
    await waitFor(() => expect(screen.getByTestId("action-row")).toBeTruthy());
    expect(api.fetchStrategy).toHaveBeenCalledWith("default");
    expect(screen.getByRole("link", { name: "Advisor" })).toHaveAttribute("href", "#advisor");
    expect(document.getElementById("advisor")).not.toBeNull();
  });

  it("saving sends the whole profile under the selected name", async () => {
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    fireEvent.blur(screen.getByLabelText("Scan interval, in battle (s)"), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(api.saveStrategy).toHaveBeenCalledWith(
        "default",
        expect.objectContaining({ interval: 5 }),
      ),
    );
  });

  it("Save is disabled until something changes", async () => {
    render(<StrategyPage />);
    await waitFor(() => screen.getByText("Save"));
    expect(screen.getByText("Save").hasAttribute("disabled")).toBe(true);
  });

  it("Revert discards local edits without calling the server", async () => {
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    fireEvent.blur(screen.getByLabelText("Scan interval, in battle (s)"), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByText("Revert"));
    await waitFor(() =>
      expect(screen.getByLabelText("Scan interval, in battle (s)").getAttribute("value")).toBe("2"),
    );
    expect(api.saveStrategy).not.toHaveBeenCalled();
  });

  it("shows the server's reason when a save is rejected", async () => {
    api.saveStrategy.mockRejectedValue(new Error("interval: must be between 0.1 and 3600"));
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    fireEvent.blur(screen.getByLabelText("Scan interval, in battle (s)"), {
      target: { value: "9999" },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(screen.getByText(/must be between/)).toBeTruthy());
  });

  it("Revert repaints a per-row threshold, not just the top-level fields", async () => {
    // Regression: the per-row threshold/brightness inputs had no remount
    // key of their own (only NumberField's top-level fields did), so a
    // Revert updated the underlying draft but left the row's DOM node
    // showing the stale, previously-typed number.
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Damage threshold"));
    fireEvent.blur(screen.getByLabelText("Damage threshold"), {
      target: { value: "0.3" },
    });
    await waitFor(() =>
      expect((screen.getByLabelText("Damage threshold") as HTMLInputElement).value).toBe("0.3"),
    );
    fireEvent.click(screen.getByText("Revert"));
    await waitFor(() =>
      expect((screen.getByLabelText("Damage threshold") as HTMLInputElement).value).toBe("0.9"),
    );
    expect(api.saveStrategy).not.toHaveBeenCalled();
  });

  it("re-fetches the strategy list when useControlSync reports another tab changed it", async () => {
    render(<StrategyPage />);
    await waitFor(() => expect(api.fetchStrategies).toHaveBeenCalledTimes(1));
    expect(sync.onChange).not.toBeNull();
    api.fetchStrategies.mockClear();
    sync.onChange!();
    await waitFor(() => expect(api.fetchStrategies).toHaveBeenCalledTimes(1));
  });

  it("switching profiles fetches the other one", async () => {
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Strategy"));
    fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "crit" } });
    await waitFor(() => expect(api.fetchStrategy).toHaveBeenCalledWith("crit"));
  });

  it("a failed load leaves the selection and the body it came from alone", async () => {
    // The whole point of committing nothing before the last await: with
    // `selected` set first, the selector would read "crit" while the editor
    // still showed default's rows, and the next Save would PUT default's
    // body under the name "crit".
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Strategy"));
    api.fetchStrategy.mockRejectedValue(new Error("crit.json is not valid JSON"));

    fireEvent.change(selector(), { target: { value: "crit" } });

    await waitFor(() => expect(screen.getByText(/not valid JSON/)).toBeTruthy());
    expect(selector().value).toBe("default");
    expect(interval().value).toBe("2");
  });

  it("a clean tab converges on a remote change to the profile it is showing", async () => {
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    api.fetchStrategy.mockResolvedValue({ ...strategy, interval: 9 });

    sync.onChange!();

    await waitFor(() => expect(interval().value).toBe("9"));
  });

  it("a dirty tab keeps its draft but re-baselines on what the server holds", async () => {
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    fireEvent.blur(interval(), { target: { value: "5" } });
    await waitFor(() => expect(interval().value).toBe("5"));

    api.fetchStrategy.mockResolvedValue({ ...strategy, interval: 9 });
    sync.onChange!();
    await waitFor(() => expect(api.fetchStrategy).toHaveBeenCalledTimes(2));

    // The edit survives - nobody's work is yanked away mid-typing.
    expect(interval().value).toBe("5");
    // But Revert now goes to what the server actually holds, not to the
    // body this tab loaded before the other tab saved over it.
    fireEvent.click(screen.getByText("Revert"));
    await waitFor(() => expect(interval().value).toBe("9"));
  });

  it("asks before discarding unsaved edits on a profile switch", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    fireEvent.blur(interval(), { target: { value: "5" } });
    await waitFor(() => expect(interval().value).toBe("5"));
    api.fetchStrategy.mockClear();

    fireEvent.change(selector(), { target: { value: "crit" } });

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(api.fetchStrategy).not.toHaveBeenCalled();
    expect(selector().value).toBe("default");
    expect(interval().value).toBe("5");
  });

  it("re-prompts on a copy name the server would refuse, instead of round-tripping", async () => {
    vi.spyOn(window, "prompt")
      .mockReturnValueOnce("has a space")
      .mockReturnValueOnce("no_space");
    render(<StrategyPage />);
    await waitFor(() => screen.getByText("Duplicate"));

    fireEvent.click(screen.getByText("Duplicate"));

    await waitFor(() => expect(api.saveStrategy).toHaveBeenCalledTimes(1));
    expect(api.saveStrategy).toHaveBeenCalledWith(
      "no_space",
      expect.objectContaining({ name: "no_space" }),
    );
    expect(window.prompt).toHaveBeenCalledTimes(2);
  });

  it("a double-clicked Save sends one PUT, not two", async () => {
    let release: (v: unknown) => void = () => {};
    api.saveStrategy.mockReturnValue(new Promise((r) => (release = r)));
    render(<StrategyPage />);
    await waitFor(() => screen.getByLabelText("Scan interval, in battle (s)"));
    fireEvent.blur(interval(), { target: { value: "5" } });

    const save = screen.getByText("Save");
    fireEvent.click(save);
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(true));
    fireEvent.click(save);

    expect(api.saveStrategy).toHaveBeenCalledTimes(1);
    release({ ...strategy, interval: 5 });
  });
});
