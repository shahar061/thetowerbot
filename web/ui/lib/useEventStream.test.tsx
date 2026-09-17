import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { EventStreamProvider, useEventStream } from "./useEventStream";

const selection = vi.hoisted(() => ({ selected: null as null | {
  key: string; running: boolean; dashboard_url: string | null;
} }));
vi.mock("./AccountSelection", () => ({ useAccountSelection: () => selection }));

const sources: FakeSource[] = [];
class FakeSource {
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  constructor(url: string) { this.url = url; sources.push(this); }
  close() { this.closed = true; }
}

function Feed() {
  const { events } = useEventStream();
  return <p>Events: {events.length}</p>;
}

beforeEach(() => {
  sources.length = 0;
  selection.selected = null;
  vi.stubGlobal("EventSource", FakeSource);
});

test("stream exists only for local running account and clears on selection change", () => {
  const view = render(<EventStreamProvider><Feed /></EventStreamProvider>);
  expect(sources).toHaveLength(0);
  selection.selected = { key: "worker:Air18", running: true, dashboard_url: window.location.origin + "/" };
  view.rerender(<EventStreamProvider><Feed /></EventStreamProvider>);
  expect(sources[0].url).toBe("/api/events/stream?scope=worker%3AAir18");
  act(() => sources[0].onmessage?.({ data: JSON.stringify({ seq: 1, type: "ScanCompleted" }) }));
  expect(screen.getByText("Events: 1")).toBeInTheDocument();
  selection.selected = { key: "unattributed", running: false, dashboard_url: null };
  view.rerender(<EventStreamProvider><Feed /></EventStreamProvider>);
  expect(sources[0].closed).toBe(true);
  expect(screen.getByText("Events: 0")).toBeInTheDocument();
  expect(sources).toHaveLength(1);
});
