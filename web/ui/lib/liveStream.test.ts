import { afterEach, describe as group, expect, it } from "vitest";
import { liveSupported, parseFrame, streamUrl } from "./liveStream";
import { frameBytes, installLiveStreamMocks, uninstallLiveStreamMocks } from "./liveStreamTesting";

afterEach(() => uninstallLiveStreamMocks());

group("streamUrl", () => {
  it("points at the worker's api/stream over ws with the scope and account", () => {
    expect(streamUrl("http://127.0.0.1:10059/", "worker:T_59", "ACC"))
      .toBe("ws://127.0.0.1:10059/api/stream?scope=worker%3AT_59&expected_account_id=ACC");
  });

  it("uses wss behind Tailscale's https and leaves out empty parameters", () => {
    expect(streamUrl("https://mac.tail.ts.net:10059/", null, null)).toBe("wss://mac.tail.ts.net:10059/api/stream");
  });
});

group("parseFrame", () => {
  it("reads the keyframe flag, the timestamp and the payload", () => {
    const frame = parseFrame(frameBytes(true, 123456, [0, 0, 0, 1, 0x65]));
    expect(frame.key).toBe(true);
    expect(frame.timestamp).toBe(123456);
    expect(Array.from(frame.data)).toEqual([0, 0, 0, 1, 0x65]);
    expect(parseFrame(frameBytes(false, 0, [])).key).toBe(false);
  });
});

group("liveSupported", () => {
  it("is false without WebCodecs", () => {
    expect(liveSupported()).toBe(false);
  });

  it("is true with WebCodecs in a secure context", () => {
    installLiveStreamMocks();
    expect(liveSupported()).toBe(true);
  });
});
