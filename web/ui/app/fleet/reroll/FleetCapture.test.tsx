import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe as group, expect, it, vi } from "vitest";
import { FleetCapture } from "./FleetCapture";
import { CONFIG_TEXT, fakeVideoFrame, MockDecoder, MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";

const props = { dashboardUrl: "http://127.0.0.1:10059/", scope: "worker:T_59", instance: "T_59", accountId: "ACC" };

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => { cleanup(); uninstallLiveStreamMocks(); vi.useRealTimers(); setHidden(false); });

group("FleetCapture", () => {
  it("shows the MJPEG snapshots where live video is not supported", () => {
    const { container } = render(<FleetCapture {...props} />);
    expect(container.querySelector("img")?.getAttribute("src"))
      .toBe("http://127.0.0.1:10059/api/frame?scope=worker%3AT_59&expected_account_id=ACC");
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("[data-feed]")).toBeNull();
    fireEvent.load(container.querySelector("img")!);
    expect(container.querySelector("[data-feed]")?.getAttribute("data-feed")).toBe("snapshots");
  });

  it("streams live, falls back to MJPEG when the stream fails, and retries after 30 s", () => {
    vi.useFakeTimers();
    installLiveStreamMocks();
    const { container } = render(<FleetCapture {...props} />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(container.querySelector("[data-feed]")).toBeNull();
    act(() => {
      MockSocket.latest().receive(CONFIG_TEXT);
      MockDecoder.latest().init.output(fakeVideoFrame());
    });
    expect(container.querySelector("[data-feed]")?.getAttribute("data-feed")).toBe("live");
    expect(MockSocket.instances).toHaveLength(1);

    act(() => MockSocket.latest().serverClose(4503));
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("img")).not.toBeNull();
    expect(container.querySelector("[data-feed]")).toBeNull();

    act(() => { vi.advanceTimersByTime(30_000); });
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(MockSocket.instances).toHaveLength(2);
  });

  it("labels the feed below the screen instead of over the game", () => {
    const { container } = render(<FleetCapture {...props} />);
    fireEvent.load(container.querySelector("img")!);
    const badge = container.querySelector("[data-feed]");
    expect(badge?.textContent).toBe("Snapshots");
    expect(container.querySelector("img")?.parentElement?.contains(badge ?? null)).toBe(false);
  });

  it("reports its feed to the live wall instead of labelling it", () => {
    installLiveStreamMocks();
    const onFeedChange = vi.fn();
    const { container, rerender } = render(<FleetCapture {...props} fill onFeedChange={onFeedChange} />);
    expect(container.querySelector("[data-feed]")).toBeNull();
    expect(onFeedChange).toHaveBeenLastCalledWith(null);
    act(() => {
      MockSocket.latest().receive(CONFIG_TEXT);
      MockDecoder.latest().init.output(fakeVideoFrame());
    });
    expect(onFeedChange).toHaveBeenLastCalledWith("live");
    act(() => MockSocket.latest().serverClose(4503));
    expect(onFeedChange).toHaveBeenLastCalledWith(null);
    fireEvent.load(container.querySelector("img")!);
    expect(onFeedChange).toHaveBeenLastCalledWith("snapshots");
    rerender(<FleetCapture {...props} fill onFeedChange={onFeedChange} paused />);
    expect(onFeedChange).toHaveBeenLastCalledWith(null);
  });

  it("fills its parent without the enlarge button on the live wall", () => {
    const { container, queryByRole } = render(<FleetCapture {...props} fill />);
    expect(container.innerHTML).not.toContain("30vh");
    expect(container.firstElementChild?.className).toContain("size-full");
    expect(queryByRole("button", { name: "Enlarge screen of T_59" })).toBeNull();
  });

  it("closes the stream while paused for the live wall", () => {
    installLiveStreamMocks();
    const { container, rerender, getByRole } = render(<FleetCapture {...props} />);
    const socket = MockSocket.latest();
    rerender(<FleetCapture {...props} paused />);
    expect(socket.closed).toBe(true);
    expect(container.querySelector("canvas")).toBeNull();
    expect(getByRole("status").textContent).toBe("Screen paused while the live wall is open.");
  });

  it("stops the stream while the tab is hidden", () => {
    installLiveStreamMocks();
    const { container } = render(<FleetCapture {...props} />);
    const socket = MockSocket.latest();
    act(() => setHidden(true));
    expect(socket.closed).toBe(true);
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("retries snapshot failures with capped backoff and resets the delay after a loaded frame", () => {
    vi.useFakeTimers();
    const { container, getByRole } = render(<FleetCapture {...props} />);
    for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      fireEvent.error(container.querySelector("img")!);
      expect(getByRole("status")).toHaveTextContent("Reconnecting");
      act(() => { vi.advanceTimersByTime(delay - 1); });
      expect(container.querySelector("img")).toBeNull();
      act(() => { vi.advanceTimersByTime(1); });
      expect(container.querySelector("img")).not.toBeNull();
    }
    fireEvent.load(container.querySelector("img")!);
    fireEvent.error(container.querySelector("img")!);
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(container.querySelector("img")).not.toBeNull();
  });

  it.each(["hidden", "paused", "offscreen"] as const)("suspends retry while %s and cleans it up when unmounted", mode => {
    vi.useFakeTimers();
    let setIntersecting: ((visible: boolean) => void) | undefined;
    if (mode === "offscreen") vi.stubGlobal("IntersectionObserver", class {
      constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
        setIntersecting = visible => callback([{ isIntersecting: visible }]);
      }
      observe(): void {}
      disconnect(): void {}
    });
    const { container, rerender, unmount } = render(<FleetCapture {...props} />);
    fireEvent.error(container.querySelector("img")!);
    act(() => {
      if (mode === "hidden") setHidden(true);
      if (mode === "offscreen") setIntersecting!(false);
    });
    if (mode === "paused") rerender(<FleetCapture {...props} paused />);
    expect(vi.getTimerCount()).toBe(0);
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(container.querySelector("img")).toBeNull();
    act(() => {
      if (mode === "hidden") setHidden(false);
      if (mode === "offscreen") setIntersecting!(true);
    });
    if (mode === "paused") rerender(<FleetCapture {...props} />);
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(container.querySelector("img")).not.toBeNull();
    fireEvent.error(container.querySelector("img")!);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("never carries a loaded frame or a failed retry into a replacement account", () => {
    vi.useFakeTimers();
    const report = vi.fn();
    const { container, rerender } = render(<FleetCapture {...props} fill onFeedChange={report} />);
    const previous = container.querySelector("img")!;
    fireEvent.load(previous);
    rerender(<FleetCapture {...props} accountId="NEW" fill onFeedChange={report} />);
    expect(container.querySelector("img")).not.toBe(previous);
    expect(container.querySelector("img")?.src).toContain("expected_account_id=NEW");
    expect(report).toHaveBeenLastCalledWith(null);
    fireEvent.error(container.querySelector("img")!);
    rerender(<FleetCapture {...props} accountId="NEWER" fill onFeedChange={report} />);
    expect(container.querySelector("img")?.src).toContain("expected_account_id=NEWER");
    expect(vi.getTimerCount()).toBe(0);
  });
});
