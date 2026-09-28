import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe as group, expect, it, vi } from "vitest";
import { LiveVideo } from "./LiveVideo";
import { CATCH_UP_DECODE_LIMIT, CAUGHT_UP_DECODE_QUEUE } from "@/lib/liveStream";
import {
  CONFIG_TEXT, LIVE_TEXT, MockDecoder, MockSocket, fakeVideoFrame, frameBytes, installLiveStreamMocks,
  uninstallLiveStreamMocks,
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
    // The "live" marker is the real protocol: it's what lets the decoder ever
    // become "caught up" in the first place (see the tests below). Here it's
    // sent right away - no replay to speak of - so the very first frame, a
    // keyframe on a fresh decoder (queue 0), is what marks the decoder caught
    // up, same as this test always assumed.
    act(() => { socket.receive(CONFIG_TEXT); socket.receive(LIVE_TEXT); socket.receive(frameBytes(true, 1, [0x65])); });
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 2, [0x41])));
    decoder.decodeQueueSize = 0;
    act(() => { socket.receive(frameBytes(false, 3, [0x41])); socket.receive(frameBytes(true, 4, [0x65])); });
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 4]);
  });

  it("decodes the whole replay burst regardless of queue depth, before the live marker arrives", () => {
    // The real failure mode: a fresh decoder reports queue 0 right when the
    // replay's first (key) frame arrives, same as it would for any single
    // ordinary frame - that's not by itself evidence the decoder is keeping
    // up with a whole backlog. Only the server's "live" marker says the
    // replay is over, and it hasn't arrived in this test at all.
    mount();
    const socket = MockSocket.latest();
    act(() => socket.receive(CONFIG_TEXT));
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = 0;
    act(() => socket.receive(frameBytes(true, 1, [0x65])));
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 2, [0x41])));
    decoder.decodeQueueSize = 200;
    act(() => socket.receive(frameBytes(false, 3, [0x41])));
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 2, 3]);
  });

  it("skips the fell-behind delta only once the live marker has arrived and the decoder has caught up", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => {
      socket.receive(CONFIG_TEXT);
      socket.receive(frameBytes(true, 1, [0x65])); // replay key
      socket.receive(frameBytes(false, 2, [0x41])); // replay delta
    });
    const decoder = MockDecoder.latest();
    act(() => socket.receive(LIVE_TEXT)); // replay is over
    decoder.decodeQueueSize = CAUGHT_UP_DECODE_QUEUE; // small: this message marks "caught up"
    act(() => socket.receive(frameBytes(false, 3, [0x41])));
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 4, [0x41])));
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 2, 3]);
  });

  it("arms the fell-behind rule for a first subscriber, whose live marker arrives before the config", () => {
    // A first subscriber (nothing cached yet) gets the hub's "live" marker
    // BEFORE the config message, since there's nothing to replay. A config
    // message must not clear replayDone - it's sent exactly once per
    // connection, and no second one is ever coming.
    mount();
    const socket = MockSocket.latest();
    act(() => { socket.receive(LIVE_TEXT); socket.receive(CONFIG_TEXT); });
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = 0; // the fresh decoder's first frame: always decoded (it's a keyframe)
    act(() => socket.receive(frameBytes(true, 1, [0x65])));
    decoder.decodeQueueSize = CAUGHT_UP_DECODE_QUEUE;
    act(() => socket.receive(frameBytes(false, 2, [0x41])));
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 3, [0x41])));
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 2]);
  });

  it("re-arms caughtUp after a mid-stream config change, without a second live marker", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => { socket.receive(CONFIG_TEXT); socket.receive(LIVE_TEXT); socket.receive(frameBytes(true, 1, [0x65])); });
    // The config rotates mid-stream (e.g. the encoder's resolution changed).
    // The hub never sends a second "live" marker for the same connection, so
    // replayDone must survive this and only caughtUp resets.
    act(() => socket.receive(CONFIG_TEXT));
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = 0; // the new decoder's first frame too reports 0
    act(() => socket.receive(frameBytes(true, 2, [0x65])));
    decoder.decodeQueueSize = CAUGHT_UP_DECODE_QUEUE;
    act(() => socket.receive(frameBytes(false, 3, [0x41])));
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 4, [0x41])));
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([2, 3]);
  });

  it("ignores a text message with an unrecognized type and leaves the decoder alone", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => socket.receive(CONFIG_TEXT));
    const decoder = MockDecoder.latest();
    const config = decoder.config;
    act(() => socket.receive(JSON.stringify({ type: "ping" })));
    expect(MockDecoder.latest()).toBe(decoder);
    expect(decoder.config).toBe(config);
    expect(decoder.state).toBe("configured");
  });

  it("skips once the queue exceeds the hard cap, even if the decoder never catches up", () => {
    // A device that's always too slow would never see a small queue, so
    // caughtUp would never become true on its own - the hard cap is what
    // still bounds the queue (and latency, and memory) in that case.
    mount();
    const socket = MockSocket.latest();
    act(() => { socket.receive(CONFIG_TEXT); socket.receive(frameBytes(true, 1, [0x65])); });
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = CATCH_UP_DECODE_LIMIT - 5; // above MAX_DECODE_QUEUE, still under the hard cap
    act(() => socket.receive(frameBytes(false, 2, [0x41])));
    decoder.decodeQueueSize = CATCH_UP_DECODE_LIMIT + 1; // now over the hard cap too
    act(() => socket.receive(frameBytes(false, 3, [0x41])));
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 2]);
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
