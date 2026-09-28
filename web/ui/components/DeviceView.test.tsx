import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe as group, expect, it } from "vitest";
import { DeviceView } from "./DeviceView";
import type { MatchBox } from "@/lib/types";
import { MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

// A non-square frame so a width/height transposition (using `width` to scale
// a `y`/`h` offset, or vice versa) would produce a wrong percentage and fail
// these assertions.
const size = { width: 200, height: 100 };

const matched: MatchBox = {
  name: "Damage", x: 100, y: 10, w: 20, h: 5, tap_x: 160, tap_y: 40, score: 0.87, tapped: false,
};
const tapped: MatchBox = {
  name: "Health", x: 20, y: 60, w: 10, h: 20, tap_x: 60, tap_y: 90, score: 0.95, tapped: true,
};

group("DeviceView", () => {
  it("positions a box at percentages of the frame's own width and height", () => {
    render(<DeviceView boxes={[matched]} size={size} />);

    const box = screen.getByTitle(/Damage 0/);
    expect(box.style.left).toBe("50%"); // 100 / 200
    expect(box.style.top).toBe("10%"); // 10 / 100
    expect(box.style.width).toBe("10%"); // 20 / 200
    expect(box.style.height).toBe("5%"); // 5 / 100
  });

  it("draws the tap point crosshair at tap_x/tap_y, not at the box's origin", () => {
    render(<DeviceView boxes={[matched]} size={size} />);

    // tap_x/tap_y (160, 40) are the buy square beside the label - the box's
    // own (x, y) origin (100, 10) is the label, which the bot never taps.
    const crosshair = screen.getByTitle(/Damage tap point/);
    expect(crosshair.style.left).toBe("80%"); // 160 / 200
    expect(crosshair.style.top).toBe("40%"); // 40 / 100
    expect(crosshair.style.left).not.toBe("50%"); // the box's own left (100 / 200)
    expect(crosshair.style.top).not.toBe("10%"); // the box's own top (10 / 100)
  });

  it("distinguishes a tapped box from a merely-matched one", () => {
    render(<DeviceView boxes={[matched, tapped]} size={size} />);

    const matchedBox = screen.getByTitle(/Damage 0/);
    const tappedBox = screen.getByTitle(/Health 0/);
    // Asserted on the state attribute rather than the class, so restyling the
    // overlay does not break a test about which box the bot actually hit.
    expect(matchedBox.getAttribute("data-state")).toBe("matched");
    expect(tappedBox.getAttribute("data-state")).toBe("tapped");
  });

  it("hides the boxes but keeps the image when the overlay is toggled off", () => {
    render(<DeviceView boxes={[matched, tapped]} size={size} />);

    expect(screen.getByTitle(/Damage 0/)).toBeDefined();
    fireEvent.click(screen.getByRole("checkbox"));

    expect(screen.queryByTitle(/Damage/)).toBeNull();
    expect(screen.queryByTitle(/Health/)).toBeNull();
    expect(screen.getByAltText("device screen")).toBeDefined();
  });

  it("shows a waiting state instead of a blank image before the first frame", () => {
    render(<DeviceView boxes={[matched, tapped]} size={null} />);

    expect(screen.getByText(/Waiting for the first frame/)).toBeDefined();
    expect(screen.queryByAltText("device screen")).toBeNull();
    expect(screen.queryByTitle(/Damage/)).toBeNull();
    expect(screen.queryByTitle(/Health/)).toBeNull();
  });
});

group("DeviceView screen source", () => {
  afterEach(() => { uninstallLiveStreamMocks(); window.localStorage.clear(); setHidden(false); });

  it("defaults to live video when supported, without the match overlay", () => {
    installLiveStreamMocks();
    const { container } = render(<DeviceView boxes={[matched]} size={size} />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(screen.queryByTitle(/Damage 0/)).toBeNull();
    expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switches to the bot's view with its match boxes and remembers the choice", () => {
    installLiveStreamMocks();
    const first = render(<DeviceView boxes={[matched]} size={size} />);
    fireEvent.click(screen.getByRole("button", { name: "Bot's view" }));
    expect(screen.getByTitle(/Damage 0/)).toBeInTheDocument();
    expect(first.container.querySelector("canvas")).toBeNull();
    first.unmount();
    render(<DeviceView boxes={[matched]} size={size} />);
    expect(screen.getByRole("button", { name: "Bot's view" })).toHaveAttribute("aria-pressed", "true");
  });

  it("falls back to the bot's view when the live stream is unavailable", () => {
    installLiveStreamMocks();
    const { container } = render(<DeviceView boxes={[matched]} size={size} />);
    act(() => MockSocket.latest().serverClose(4503));
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.getByTitle(/Damage 0/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bot's view" })).toHaveAttribute("aria-pressed", "true");
  });

  it("has no toggle where live video is not supported", () => {
    render(<DeviceView boxes={[matched]} size={size} />);
    expect(screen.queryByRole("group", { name: "Screen source" })).toBeNull();
  });

  it("closes the live stream and falls back to the bot's view while the tab is hidden", () => {
    installLiveStreamMocks();
    const { container } = render(<DeviceView boxes={[matched]} size={size} />);
    const socket = MockSocket.latest();
    act(() => setHidden(true));
    expect(socket.closed).toBe(true);
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.getByTitle(/Damage 0/)).toBeInTheDocument();
  });
});
