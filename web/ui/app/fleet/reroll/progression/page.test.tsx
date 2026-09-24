import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { RerollMember } from "@/lib/fleet";
import type { MilestoneRoadmap } from "@/lib/milestoneRoadmap";
import ProgressionPage from "./page";
const state = vi.hoisted(() => ({ members: [] as RerollMember[], fetcher: vi.fn(), query: "" }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => ({ pool: { members: state.members }, loading: false, error: null }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(state.query) }));
vi.mock("@/lib/api", () => ({ fetchAccountRoadmap: state.fetcher }));
const member: RerollMember = { name: "Air_1", account_key: "worker:Air_1", account_id: "100", endpoint: "", lease_id: "a", state: "running" };
const roadmap: MilestoneRoadmap = { schema_version: 1, account_id: "100", best_waves: {}, nodes: [{ id: "lab", title: "Laboratory", description: "", group: "Features", kind: "unlock", tier: 1, wave: 20, requires: [], source_url: "", status: "unknown", progress: null }] };
beforeEach(() => { state.query = ""; state.members = [member]; state.fetcher.mockReset().mockResolvedValue(roadmap); });
test("a replaced account never renders the previous account's late response", async () => {
  let resolve!: (value: MilestoneRoadmap) => void;
  state.fetcher.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const view = render(<ProgressionPage />);
  await waitFor(() => expect(state.fetcher).toHaveBeenCalledTimes(1));
  state.members = [{ ...member, account_id: "200" }];
  state.fetcher.mockResolvedValue({ ...roadmap, account_id: "200", nodes: [] });
  view.rerender(<ProgressionPage />);
  await waitFor(() => expect(state.fetcher).toHaveBeenCalledTimes(2));
  resolve(roadmap);
  await waitFor(() => expect(screen.getByText(/No milestone catalog available/)).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: /Select Laboratory/ })).not.toBeInTheDocument();
});
test("renders individual unavailable worker states", async () => {
  state.members = [member, { ...member, name: "Air_2", account_key: "worker:Air_2", account_id: "200" }];
  state.fetcher.mockImplementation(async (key: string) => { if (key.endsWith("2")) throw new Error("Worker offline"); return roadmap; });
  render(<ProgressionPage />);
  expect(await screen.findByRole("button", { name: /Select Laboratory/ })).toBeInTheDocument();
  expect(screen.getByText("Worker offline")).toBeInTheDocument();
});

vi.mock("./atlas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));

test("a slow worker does not delay healthy worker milestones", async () => {
  state.members = [member, { ...member, name: "Air_2", account_key: "worker:Air_2", account_id: "200" }];
  state.fetcher.mockImplementation((key: string) => key.endsWith("2") ? new Promise(() => {}) : Promise.resolve(roadmap));
  render(<ProgressionPage />);
  expect(await screen.findByRole("button", { name: /Select Laboratory/ })).toBeInTheDocument();
  expect(screen.getByText("Awaiting evidence")).toBeInTheDocument();
});

afterEach(() => { vi.useRealTimers(); });

test("polls only after the prior read completes and stops on unmount", async () => {
  vi.useFakeTimers();
  let resolve!: (value: MilestoneRoadmap) => void;
  state.fetcher.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const view = render(<ProgressionPage />);
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(state.fetcher).toHaveBeenCalledTimes(1);
  await act(async () => { resolve(roadmap); });
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(state.fetcher).toHaveBeenCalledTimes(2);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(state.fetcher).toHaveBeenCalledTimes(2);
});

test("focuses a linked worker only when worker, account scope, and identity match", async () => {
  state.query = "worker=Air_1&account=worker%3AAir_1&identity=100";
  const view = render(<ProgressionPage />);
  expect(await screen.findByRole("region", { name: "Strategy intent" })).toHaveTextContent("Air_1");
  state.query = "worker=Air_1&account=worker%3AAir_1&identity=old";
  view.rerender(<ProgressionPage />);
  expect(screen.queryByRole("region", { name: "Strategy intent" })).not.toBeInTheDocument();
});
