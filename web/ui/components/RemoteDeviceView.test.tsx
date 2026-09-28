import { act, render, screen } from "@testing-library/react";
import { afterEach, describe as group, expect, it } from "vitest";
import { RemoteDeviceView } from "./RemoteDeviceView";
import { MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => { uninstallLiveStreamMocks(); setHidden(false); });

group("RemoteDeviceView", () => {
  it("streams the remote worker live over wss behind Tailscale", () => {
    installLiveStreamMocks();
    const { container } = render(<RemoteDeviceView dashboardUrl="https://mac.tail.ts.net:10059/" scope="worker:T_59" instance="T_59" />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(MockSocket.latest().url).toBe("wss://mac.tail.ts.net:10059/api/stream?scope=worker%3AT_59");
  });

  it("stops the stream while the tab is hidden", () => {
    installLiveStreamMocks();
    const { container } = render(<RemoteDeviceView dashboardUrl="https://mac.tail.ts.net:10059/" scope="worker:T_59" instance="T_59" />);
    const socket = MockSocket.latest();
    act(() => setHidden(true));
    expect(socket.closed).toBe(true);
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("paused");
  });
});
