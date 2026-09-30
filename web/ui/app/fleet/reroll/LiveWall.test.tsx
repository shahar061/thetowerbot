import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe as group, expect, it, vi } from "vitest";
import type { AccountChoice } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { enterWallFullscreen, LiveWall } from "./LiveWall";

const member = (name: string, extra: Partial<RerollMember> = {}): RerollMember => ({
  name, endpoint: "127.0.0.1:5555", lease_id: `${name}-lease`, state: "running", account_id: `${name}-id`, account_key: `${name}-key`, ...extra,
});
const account = (name: string, extra: Partial<AccountChoice> = {}): AccountChoice => ({
  key: `${name}-key`, account_id: `${name}-id`, instance: name, kind: "worker", running: true,
  dashboard_url: `http://127.0.0.1:1${name.length}000/`, ...extra,
});

group("LiveWall", () => {
  it("keeps named placeholders for unverified workers without opening their feed", () => {
    render(<LiveWall members={[member("bs-3"), member("bs-1"), member("bs-2")]}
      accounts={[account("bs-3"), account("bs-1"), account("bs-2", { running: false })]} onClose={vi.fn()} />);
    expect(screen.getAllByRole("img").map(image => image.getAttribute("alt")))
      .toEqual(["Live screen of bs-1", "Live screen of bs-3"]);
    const unverified = screen.getByRole("region", { name: "bs-2 live screen" });
    expect(within(unverified).getByRole("status")).toHaveTextContent("not verified");
    expect(within(unverified).queryByRole("img")).toBeNull();
    expect(screen.getAllByRole("region").map(region => region.getAttribute("aria-label")))
      .toEqual(["bs-1 live screen", "bs-2 live screen", "bs-3 live screen"]);
  });

  it("labels each tile with its name, tier, wave and state", () => {
    render(<LiveWall members={[member("bs-2", { tier: 8, wave: 1432 }), member("bs-4")]}
      accounts={[account("bs-2"), account("bs-4")]} onClose={vi.fn()} />);
    expect(screen.getByText("bs-2 · T8 · W1432 · Running")).toBeTruthy();
    expect(screen.getByText("bs-4 · Running")).toBeTruthy();
    const caption = screen.getByRole("heading", { name: /^bs-2/ });
    expect(caption.querySelector("[data-feed]")).toBeNull();
    fireEvent.load(screen.getByRole("img", { name: "Live screen of bs-2" }));
    expect(caption.querySelector("[data-feed]")?.textContent).toBe("Snapshots");
  });

  it("says so when no emulator has a live screen", () => {
    render(<LiveWall members={[]} accounts={[]} onClose={vi.fn()} />);
    expect(screen.getByRole("status").textContent).toContain("No emulator has a live screen");
  });

  it("closes on Escape and on the close button", () => {
    const onClose = vi.fn();
    render(<LiveWall members={[member("bs-1")]} accounts={[account("bs-1")]} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Close live wall" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("keeps the overlay when fullscreen has never been entered", () => {
    const onClose = vi.fn();
    render(<LiveWall members={[member("bs-1")]} accounts={[]} onClose={onClose} />);
    fireEvent(document, new Event("fullscreenchange"));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Live wall" })).toBeInTheDocument();
  });

  it.each(["throw", "reject"])("preserves the overlay when fullscreen requests %s", async mode => {
    const request = vi.fn(() => {
      if (mode === "throw") throw new Error("Fullscreen unavailable");
      return Promise.reject(new Error("Fullscreen unavailable"));
    });
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { value: request, configurable: true });
    try {
      expect(() => enterWallFullscreen()).not.toThrow();
      const onClose = vi.fn();
      render(<LiveWall members={[member("bs-1")]} accounts={[]} onClose={onClose} />);
      await act(async () => {});
      fireEvent(document, new Event("fullscreenchange"));
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      delete (HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen;
    }
  });

  it("removes an old account feed immediately and keeps the worker visible during re-verification", () => {
    const { rerender } = render(<LiveWall members={[member("bs-1")]} accounts={[account("bs-1")]} onClose={vi.fn()} />);
    const previous = screen.getByRole("img");
    fireEvent.load(previous);
    rerender(<LiveWall members={[member("bs-1", { account_id: "new-id" })]}
      accounts={[account("bs-1")]} onClose={vi.fn()} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("region", { name: "bs-1 live screen" })).toHaveTextContent("not verified");
    expect(screen.queryByText("Snapshots")).toBeNull();
    rerender(<LiveWall members={[member("bs-1", { account_id: "new-id" })]}
      accounts={[account("bs-1", { account_id: "new-id" })]} onClose={vi.fn()} />);
    expect(screen.getByRole("img")).not.toBe(previous);
    expect(screen.getByRole("img").getAttribute("src")).toContain("expected_account_id=new-id");
  });

  it("closes when the browser leaves full screen, and leaves it when closed", () => {
    const onClose = vi.fn();
    const setFullscreen = (element: Element | null) =>
      Object.defineProperty(document, "fullscreenElement", { value: element, configurable: true });
    const request = vi.fn(function (this: Element) { setFullscreen(this); return Promise.resolve(); });
    const exit = vi.fn(() => { setFullscreen(null); return Promise.resolve(); });
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { value: request, configurable: true });
    Object.defineProperty(document, "exitFullscreen", { value: exit, configurable: true });
    try {
      enterWallFullscreen();
      expect(document.fullscreenElement).toBe(document.documentElement);
      const view = render(<LiveWall members={[member("bs-1")]} accounts={[account("bs-1")]} onClose={onClose} />);
      fireEvent(document, new Event("fullscreenchange"));
      expect(onClose).not.toHaveBeenCalled();
      view.unmount();
      expect(exit).toHaveBeenCalledTimes(1);

      enterWallFullscreen();
      render(<LiveWall members={[member("bs-1")]} accounts={[account("bs-1")]} onClose={onClose} />);
      setFullscreen(null);
      fireEvent(document, new Event("fullscreenchange"));
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      delete (document as { exitFullscreen?: unknown }).exitFullscreen;
      delete (HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen;
      Object.defineProperty(document, "fullscreenElement", { value: null, configurable: true });
    }
  });
});
