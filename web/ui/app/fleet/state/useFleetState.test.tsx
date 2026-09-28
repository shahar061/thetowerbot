import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { makeAccount, makePayload } from "./fixtures";
import { useFleetState, type FleetStateView } from "./useFleetState";

const fetchState = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchFleetState: fetchState }));

const seen: FleetStateView[] = [];
function Probe(): React.JSX.Element {
  const view = useFleetState();
  seen.push(view);
  return <div data-testid="probe">{view.accounts.map(a => `${a.id}#${a.scan}`).join(",")}|{view.connectionLost ? "lost" : "ok"}</div>;
}

beforeEach(() => { vi.useFakeTimers(); fetchState.mockReset(); seen.length = 0; });
afterEach(() => { vi.useRealTimers(); });

test("polls every 2 s, keeps the last payload when a poll fails, and reuses unchanged accounts", async () => {
  const account = () => makeAccount({ id: "Air_1", scan: 5, online: true });
  fetchState.mockResolvedValueOnce(makePayload([account()]))
    .mockRejectedValueOnce(new Error("down"))
    .mockResolvedValueOnce(makePayload([account()]))
    .mockResolvedValueOnce(makePayload([makeAccount({ id: "Air_1", scan: 6, online: true })]));
  const view = render(<Probe />);
  await act(async () => {});
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#5|ok");
  const first = seen.at(-1)!.accounts[0];
  const firstChanged = seen.at(-1)!.changedAt.Air_1;

  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(fetchState).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#5|lost");

  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#5|ok");
  expect(seen.at(-1)!.accounts[0]).toBe(first);
  expect(seen.at(-1)!.changedAt.Air_1).toBe(firstChanged);

  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#6|ok");
  expect(seen.at(-1)!.accounts[0]).not.toBe(first);

  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(fetchState).toHaveBeenCalledTimes(4);
});
