import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { NewRerollDialog } from "./NewRerollDialog";
import type { RerollCandidate, RerollMember, RerollRun } from "@/lib/fleet";

const run: RerollRun = { number: 2, name: "Reroll #2", status: "active", started_at: "2026-09-23T09:00:00Z" };
const members: RerollMember[] = [
  { name: "Air_1", endpoint: "e1", lease_id: "a", state: "running", account_id: "100", wave: 42 },
  { name: "Air_2", endpoint: "e2", lease_id: "b", state: "paused", account_id: "200" },
];
const candidates: RerollCandidate[] = [
  { name: "Air_3", endpoint: "e3", state: "ready" },
  { name: "Air_4", endpoint: "e4", state: "retired" },
];

function renderDialog(overrides: Partial<Parameters<typeof NewRerollDialog>[0]> = {}) {
  const props = { open: true, onClose: vi.fn(), run, members, candidates, busy: false, error: null,
    onConfirm: vi.fn(), ...overrides };
  render(<NewRerollDialog {...props} />);
  return props;
}

test("warns first that the previous reroll closes for good and stays viewable", async () => {
  const props = renderDialog();
  expect(await screen.findByRole("alertdialog", { name: "Close Reroll #2?" })).toBeInTheDocument();
  expect(screen.getByText(/closes Reroll #2 for good/)).toBeInTheDocument();
  expect(screen.getByText(/can't be played again/)).toBeInTheDocument();
  expect(screen.getByText(/view their data/)).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(props.onClose).toHaveBeenCalled();
  expect(props.onConfirm).not.toHaveBeenCalled();
});

test("selection step counts keep, add and retire and confirms the exact choice", async () => {
  const props = renderDialog();
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  expect(screen.getByRole("button", { name: "Start Reroll #3 and retire 2" })).toBeDisabled();

  fireEvent.click(screen.getByRole("checkbox", { name: /Air_1/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_3/ }));
  expect(screen.getByRole("checkbox", { name: /Air_4/ })).toBeDisabled();
  expect(screen.getByText((_, node) => node?.tagName === "P" && node.textContent === "Keep 1 · Add 1 · Retire 1")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Start Reroll #3 and retire 1" }));
  expect(props.onConfirm).toHaveBeenCalledWith({ keep: ["Air_1"], add: ["Air_3"] });
});

test("a background poll delivering a fresh run object keeps the operator's choices", async () => {
  const props = { open: true, onClose: vi.fn(), run, members, candidates, busy: false, error: null, onConfirm: vi.fn() };
  const { rerender } = render(<NewRerollDialog {...props} />);
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_1/ }));
  rerender(<NewRerollDialog {...props} run={{ ...run }} />);
  expect(screen.getByRole("checkbox", { name: /Air_1/ })).toBeChecked();
});

test("a selection that goes stale after a poll is dropped from the summary, the button and the confirm payload", async () => {
  const props = { open: true, onClose: vi.fn(), run, members, candidates, busy: false, error: null, onConfirm: vi.fn() };
  const { rerender } = render(<NewRerollDialog {...props} />);
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_1/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_3/ }));

  const staleCandidates: RerollCandidate[] = [
    { name: "Air_3", endpoint: "e3", state: "retired" },
    { name: "Air_4", endpoint: "e4", state: "retired" },
  ];
  rerender(<NewRerollDialog {...props} candidates={staleCandidates} />);

  expect(screen.getByText((_, node) => node?.tagName === "P" && node.textContent === "Keep 1 · Add 0 · Retire 1")).toBeInTheDocument();
  const confirmButton = screen.getByRole("button", { name: "Start Reroll #3 and retire 1" });
  fireEvent.click(confirmButton);
  expect(props.onConfirm).toHaveBeenCalledWith({ keep: ["Air_1"], add: [] });
});

test("a server refusal is shown inside the dialog", async () => {
  renderDialog({ error: "instance_retired" });
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  expect(screen.getByRole("alert")).toHaveTextContent("instance retired");
});

test("the first reroll skips the warning and has no keep list", async () => {
  renderDialog({ run: null, members: [] });
  expect(await screen.findByRole("alertdialog", { name: "Start a reroll" })).toBeInTheDocument();
  expect(screen.queryByText(/Keep playing/)).toBeNull();
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_3/ }));
  expect(screen.getByRole("button", { name: "Start Reroll #1" })).toBeEnabled();
});
