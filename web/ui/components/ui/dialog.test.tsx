import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ConfirmDialog } from "./dialog";

test("renders an accessible alert dialog only while open and closes on Escape", async () => {
  const onOpenChange = vi.fn();
  const { rerender } = render(<ConfirmDialog open={false} onOpenChange={onOpenChange} title="Close Reroll #2?" footer={<button>OK</button>}>Body</ConfirmDialog>);
  expect(screen.queryByRole("alertdialog")).toBeNull();

  rerender(<ConfirmDialog open onOpenChange={onOpenChange} title="Close Reroll #2?" tone="warn" footer={<button>OK</button>}>Body text</ConfirmDialog>);
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveAccessibleName("Close Reroll #2?");
  expect(screen.getByText("Body text")).toBeInTheDocument();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(onOpenChange).toHaveBeenCalledWith(false, expect.anything());
});
