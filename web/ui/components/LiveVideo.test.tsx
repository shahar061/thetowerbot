import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe as group, expect, it, vi } from "vitest";
import { LiveVideo } from "./LiveVideo";
import {
  CONFIG_TEXT, MockDecoder, MockSocket, fakeVideoFrame, frameBytes, installLiveStreamMocks, uninstallLiveStreamMocks,
} from "@/lib/liveStreamTesting";

let drawImage: ReturnType<typeof vi.fn>;
beforeEach(() => { ({ drawImage } = installLiveStreamMocks()); });
afterEach(() => { uninstallLiveStreamMocks(); vi.useRealTimers(); });

function mount() {
  const onUnavailable = vi.fn();
  const onFrame = vi.fn();
  const view = render(<LiveVideo dashboardUrl="http://127.0.0.1:10059/" scope="worker:T_59" expectedAccountId="ACC"
    label="Live screen of T_59" onUnavailable={onUnavailable} onFrame={onFrame} />);
  return { ...view, onUnavailable, onFrame };
}

group("LiveVideo", () => {
  it("opens the worker's stream socket for the scoped account", () => {
    mount();
    expect(MockSocket.latest().url).toBe("ws://127.0.0.1:10059/api/stream?scope=worker%3AT_59&expected_account_id=ACC");
    expect(MockSocket.latest().binaryType).toBe("arraybuffer");
  });

  it("configures the decoder from the config message and decodes from the first keyframe on", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => socket.receive(CONFIG_TEXT));
    const decoder = MockDecoder.latest();
    expect(decoder.config).toMatchObject({ codec: "avc1.42C029", codedWidth: 576, codedHeight: 1280 });
    act(() => {
      socket.receive(frameBytes(false, 1, [0, 0, 0, 1, 0x41])); // no keyframe yet: undecodable
      socket.receive(frameBytes(true, 2, [0, 0, 0, 1, 0x65]));
      socket.receive(frameBytes(false, 3, [0, 0, 0, 1, 0x41]));
    });
    expect(decoder.chunks.map(chunk => [chunk.init.type, chunk.init.timestamp])).toEqual([["key", 2], ["delta", 3]]);
  });

  it("draws each decoded frame, releases it, and reports only the first", () => {
    const { onFrame } = mount();
    act(() => MockSocket.latest().receive(CONFIG_TEXT));
    const frame = fakeVideoFrame();
    act(() => { MockDecoder.latest().init.output(frame); MockDecoder.latest().init.output(fakeVideoFrame()); });
    expect(drawImage).toHaveBeenCalledWith(frame, 0, 0);
    expect(frame.close).toHaveBeenCalled();
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "Live screen of T_59" })).toHaveAttribute("width", "576");
  });

  it.each([4503, 1001, 4409, 4403, 1006])("falls back when the stream closes with %i", code => {
    const { onUnavailable } = mount();
    act(() => MockSocket.latest().serverClose(code));
    expect(onUnavailable).toHaveBeenCalledTimes(1);
  });

  it("falls back on a decoder error and closes the socket", () => {
    const { onUnavailable } = mount();
    act(() => MockSocket.latest().receive(CONFIG_TEXT));
    act(() => MockDecoder.latest().init.error(new Error("bad bitstream")));
    expect(onUnavailable).toHaveBeenCalledTimes(1);
    expect(MockSocket.latest().closed).toBe(true);
  });

  it("falls back when no frame is drawn within five seconds", () => {
    vi.useFakeTimers();
    const { onUnavailable } = mount();
    act(() => { vi.advanceTimersByTime(4_999); });
    expect(onUnavailable).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(onUnavailable).toHaveBeenCalledTimes(1);
  });

  it("skips to the next keyframe when the decoder falls behind", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => { socket.receive(CONFIG_TEXT); socket.receive(frameBytes(true, 1, [0x65])); });
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 2, [0x41])));
    decoder.decodeQueueSize = 0;
    act(() => { socket.receive(frameBytes(false, 3, [0x41])); socket.receive(frameBytes(true, 4, [0x65])); });
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 4]);
  });

  it("closes quietly on unmount", () => {
    const { unmount, onUnavailable } = mount();
    act(() => MockSocket.latest().receive(CONFIG_TEXT));
    unmount();
    expect(MockSocket.latest().closed).toBe(true);
    expect(MockDecoder.latest().state).toBe("closed");
    expect(onUnavailable).not.toHaveBeenCalled();
  });
});
