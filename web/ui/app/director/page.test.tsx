import { render, screen } from "@testing-library/react";
import { describe as group, expect, it, vi } from "vitest";
import DirectorPage from "./page";

const { fetchDirector } = vi.hoisted(() => ({ fetchDirector: vi.fn() }));

vi.mock("@/lib/api", () => ({ fetchDirector }));

function candidate(overrides = {}) {
  return {
    objective_id: "tier.2.unlock", status: "ready", score: 0.5,
    hours_to_afford: { kind: "unknown" }, price: null, currency: null,
    blocked_by: [], held_by: [], why: "tier.2.unlock is ready to attempt.",
    knowledge_refs: [{ id: "tier.2.unlock_wave", source_url: "https://wiki.example/tier-2" }],
    ...overrides,
  };
}

function payload(overrides = {}) {
  return {
    observed_at: null, revision_id: null, reason: "No objective is both ready and unheld right now.",
    top: null, candidates: [],
    ...overrides,
  };
}

group("DirectorPage", () => {
  it("shows an explicit empty state with nothing to rank", async () => {
    fetchDirector.mockResolvedValue(payload());
    render(<DirectorPage />);

    await screen.findByText("Nothing to rank yet.");
  });

  it("does not report an unreachable server as an empty plan", async () => {
    fetchDirector.mockRejectedValue(new Error("/api/director -> 503"));
    render(<DirectorPage />);

    await screen.findByText(/could not load/i);
    expect(screen.queryByText("Nothing to rank yet.")).toBeNull();
  });

  it("lists a candidate with its knowledge citation as a link", async () => {
    fetchDirector.mockResolvedValue(payload({ candidates: [candidate()] }));
    render(<DirectorPage />);

    await screen.findByText("tier.2.unlock");
    const link = screen.getByRole("link", { name: "tier.2.unlock_wave" });
    expect(link).toHaveAttribute("href", "https://wiki.example/tier-2");
  });

  it("renders an unknown horizon and a measured-infinite one differently", async () => {
    fetchDirector.mockResolvedValue(payload({
      candidates: [
        candidate({ objective_id: "a.unknown", hours_to_afford: { kind: "unknown" } }),
        candidate({ objective_id: "a.infinite", hours_to_afford: { kind: "infinite" } }),
        candidate({ objective_id: "a.hours", hours_to_afford: { kind: "hours", hours: 12.3 } }),
      ],
    }));
    render(<DirectorPage />);

    await screen.findByText("unknown");
    expect(screen.getByText("never (measured)")).toBeDefined();
    expect(screen.getByText("12.30h")).toBeDefined();
  });

  it("shows the held reason in full, not just a boolean", async () => {
    fetchDirector.mockResolvedValue(payload({
      candidates: [
        candidate({
          objective_id: "uw.slot.1",
          held_by: ["this is a one-way, irreversible choice and the strategy's arming ladder declares no matching pre-approval for it"],
        }),
      ],
    }));
    render(<DirectorPage />);

    await screen.findByText(/one-way, irreversible choice/);
  });

  it("shows the top pick's own explanation when there is one", async () => {
    fetchDirector.mockResolvedValue(payload({
      top: candidate({ objective_id: "labs.unlocked", why: "labs.unlocked is ready to attempt." }),
    }));
    render(<DirectorPage />);

    await screen.findByText("labs.unlocked is ready to attempt.");
  });

  it("shows the plan's reason when nothing is top", async () => {
    fetchDirector.mockResolvedValue(payload({ reason: "PLAN_REASON_TOKEN", top: null }));
    render(<DirectorPage />);

    await screen.findByText("PLAN_REASON_TOKEN");
  });

  it("shows the plan's reason (the census and income state) even when there is a top pick", async () => {
    // Regression: this page used to render `data.reason` only in the
    // `top === null` branch, so the ready/blocked/held census and
    // CurrencyRates.reason - the only place a reader can tell whether
    // income has ever been measured, since the real graph has no
    // coins-priced candidate to quote it in `why` - were silently
    // discarded on every day a top pick existed, which is most days.
    fetchDirector.mockResolvedValue(payload({
      top: candidate({ objective_id: "labs.unlocked", why: "labs.unlocked is ready to attempt." }),
      reason: "Top pick: labs.unlocked is ready to attempt. CENSUS_AND_INCOME_TOKEN",
    }));
    render(<DirectorPage />);

    await screen.findByText("labs.unlocked is ready to attempt.");
    expect(screen.getByText(/CENSUS_AND_INCOME_TOKEN/)).toBeDefined();
  });
});
