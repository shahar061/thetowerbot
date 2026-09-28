import { act, render } from "@testing-library/react";
import { afterEach, describe as group, expect, it, vi } from "vitest";
import { FleetCapture } from "./FleetCapture";
import { MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";

const props = { dashboardUrl: "http://127.0.0.1:10059/", scope: "worker:T_59", instance: "T_59", accountId: "ACC" };

afterEach(() => { uninstallLiveStreamMocks(); vi.useRealTimers(); });

group("FleetCapture", () => {
  it("shows the MJPEG snapshots where live video is not supported", () => {
    const { container } = render(<FleetCapture {...props} />);
    expect(container.querySelector("img")?.getAttribute("src"))
      .toBe("http://127.0.0.1:10059/api/frame?scope=worker%3AT_59&expected_account_id=ACC");
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("[data-feed]")?.getAttribute("data-feed")).toBe("snapshots");
  });

  it("streams live, falls back to MJPEG when the stream fails, and retries after 30 s", () => {
    vi.useFakeTimers();
    installLiveStreamMocks();
    const { container } = render(<FleetCapture {...props} />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(container.querySelector("[data-feed]")?.getAttribute("data-feed")).toBe("live");
    expect(MockSocket.instances).toHaveLength(1);

    act(() => MockSocket.latest().serverClose(4503));
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("img")).not.toBeNull();

    act(() => { vi.advanceTimersByTime(30_000); });
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(MockSocket.instances).toHaveLength(2);
  });
});
