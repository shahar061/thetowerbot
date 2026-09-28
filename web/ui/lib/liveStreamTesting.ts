import { vi } from "vitest";

/** Stand-ins for WebSocket / VideoDecoder / EncodedVideoChunk, which jsdom lacks. */
export class MockSocket {
  static instances: MockSocket[] = [];
  binaryType = "blob";
  readyState = 0;
  closed = false;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  constructor(public url: string) { MockSocket.instances.push(this); }
  close(): void { this.closed = true; this.readyState = 3; }
  receive(data: unknown): void { this.onmessage?.({ data }); }
  serverClose(code: number): void { this.readyState = 3; this.onclose?.({ code }); }
  static latest(): MockSocket {
    const socket = MockSocket.instances.at(-1);
    if (!socket) throw new Error("no socket was opened");
    return socket;
  }
}

export class MockChunk {
  constructor(public init: { type: "key" | "delta"; timestamp: number; data: Uint8Array }) {}
}

export class MockDecoder {
  static instances: MockDecoder[] = [];
  state: "unconfigured" | "configured" | "closed" = "unconfigured";
  decodeQueueSize = 0;
  config: unknown = null;
  chunks: MockChunk[] = [];
  constructor(public init: { output: (frame: unknown) => void; error: (error: unknown) => void }) {
    MockDecoder.instances.push(this);
  }
  configure(config: unknown): void { this.config = config; this.state = "configured"; }
  decode(chunk: MockChunk): void { this.chunks.push(chunk); }
  close(): void { this.state = "closed"; }
  static latest(): MockDecoder {
    const decoder = MockDecoder.instances.at(-1);
    if (!decoder) throw new Error("no decoder was created");
    return decoder;
  }
}

export const CONFIG_TEXT = JSON.stringify({ type: "config", codec: "avc1.42C029", width: 576, height: 1280 });

export function frameBytes(key: boolean, timestamp: number, payload: number[]): ArrayBuffer {
  const buffer = new ArrayBuffer(9 + payload.length);
  const view = new DataView(buffer);
  view.setUint8(0, key ? 1 : 0);
  view.setBigUint64(1, BigInt(timestamp));
  new Uint8Array(buffer, 9).set(payload);
  return buffer;
}

export function fakeVideoFrame(): { displayWidth: number; displayHeight: number; close: ReturnType<typeof vi.fn> } {
  return { displayWidth: 576, displayHeight: 1280, close: vi.fn() };
}

const secureContext = Object.getOwnPropertyDescriptor(window, "isSecureContext");

export function installLiveStreamMocks(): { drawImage: ReturnType<typeof vi.fn> } {
  MockSocket.instances = [];
  MockDecoder.instances = [];
  vi.stubGlobal("WebSocket", MockSocket);
  vi.stubGlobal("VideoDecoder", MockDecoder);
  vi.stubGlobal("EncodedVideoChunk", MockChunk);
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as never);
  return { drawImage };
}

export function uninstallLiveStreamMocks(): void {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  if (secureContext) Object.defineProperty(window, "isSecureContext", secureContext);
  else delete (window as { isSecureContext?: boolean }).isSecureContext;
}
