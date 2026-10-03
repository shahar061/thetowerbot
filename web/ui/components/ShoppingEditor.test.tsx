import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ShoppingEditor } from "./ShoppingEditor";
import type { Shopping } from "@/lib/types";

const policy: Shopping = {
  enabled: true,
  armed: false,
  visit_every_n_runs: 1,
  max_taps_per_visit: 40,
  workshop: [
    { name: "Unlock Cash Bonuses", category: "UTILITY", enabled: true },
    { name: "Damage", category: "ATTACK", enabled: true },
  ],
  cards: { enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" },
};

describe("ShoppingEditor", () => {
  it("defaults to no Workshop spending and edits the budget without arming", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={policy} onChange={onChange} />);
    expect(screen.getByLabelText("Coin budget per visit")).toHaveValue(0);
    fireEvent.blur(screen.getByLabelText("Coin budget per visit"), { target: { value: "200" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ coin_budget: 200, armed: false }));
  });
  it("reads an empty coin budget as unlimited", () => {
    render(<ShoppingEditor shopping={{ ...policy, coin_budget: null }} onChange={vi.fn()} />);
    expect(screen.getByLabelText("Coin budget per visit")).toHaveValue(null);
    expect(screen.getByText("(unlimited)")).toBeInTheDocument();
  });

  it("commits a cleared coin budget as unlimited, not as zero", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={{ ...policy, coin_budget: 200 }} onChange={onChange} />);
    fireEvent.blur(screen.getByLabelText("Coin budget per visit"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ coin_budget: null }));
  });

  it("commits a wallet share as a fraction of the balance", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={policy} onChange={onChange} />);
    fireEvent.blur(screen.getByLabelText("Coin budget share per visit"), { target: { value: "25" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ coin_budget_pct: 0.25 }));
  });

  it("shows a stored wallet share as a percentage", () => {
    render(<ShoppingEditor shopping={{ ...policy, coin_budget_pct: 0.25 }} onChange={vi.fn()} />);
    expect(screen.getByLabelText("Coin budget share per visit")).toHaveValue(25);
  });

  it("commits a cleared wallet share as none, not as zero", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={{ ...policy, coin_budget_pct: 0.25 }} onChange={onChange} />);
    fireEvent.blur(screen.getByLabelText("Coin budget share per visit"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ coin_budget_pct: null }));
  });

  it("shows rows in priority order", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    const rows = screen.getAllByTestId("shopping-row");
    expect(rows[0]).toHaveTextContent("Unlock Cash Bonuses");
  });

  it("says plainly that an unarmed policy spends nothing", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    expect(screen.getByText(/rehearsal/i)).toBeInTheDocument();
  });

  it("requires a confirmation before arming", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={policy} onChange={onChange} />);
    fireEvent.click(screen.getByRole("switch", { name: /arm/i }));
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /yes, spend coins/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ armed: true }),
    );
  });

  it("disarms without asking", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={{ ...policy, armed: true }} onChange={onChange} />);
    fireEvent.click(screen.getByRole("switch", { name: /arm/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ armed: false }));
  });

  it("carries a hint explaining why each row sits where it does", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    // Anchored in the hint's own text, not the row name: "Unlock Cash
    // Bonuses" the row is named for what it does, but its hint ("Opens the
    // whole Utility tab...") never says "unlock" - a test that claims to
    // check hint content must fail if the hint content is wrong.
    expect(screen.getByTestId("hint-Unlock Cash Bonuses")).toHaveTextContent(/Utility tab/i);
  });

  it("shows a row as a name, a category and a switch - nothing else", () => {
    // A row is addressed by the name OCR reads off the page now, so there is
    // no template to match, no match threshold to tune and no layout to
    // record. Leaving the threshold input here would be worse than untidy:
    // its onBlur PATCHes `threshold` back, and the server rejects a row
    // field it no longer knows.
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    const row = screen.getAllByTestId("shopping-row")[0];
    expect(row).toHaveTextContent("Unlock Cash Bonuses");
    expect(row).toHaveTextContent(/utility/i);
    expect(row).not.toHaveTextContent(/layout/i);
    expect(screen.queryByLabelText(/threshold/i)).toBeNull();
  });

  it("has no brightness control on a row - shopping.py has no brightness path", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    expect(screen.queryByLabelText(/brightness/i)).toBeNull();
  });

  it("says why nothing will happen when this machine cannot read a balance", () => {
    render(
      <ShoppingEditor
        shopping={policy}
        onChange={vi.fn()}
        disabledReason="header atlas is missing 2, 3, 5, 6, 9"
      />,
    );
    expect(screen.getByText(/header atlas is missing 2, 3, 5, 6, 9/)).toBeInTheDocument();
  });

  it("says nothing about being disabled when the machine is fine", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    expect(screen.queryByText(/cannot run on this machine/i)).toBeNull();
  });

  it("marks the arm confirmation as an assertive alert dialog and focuses Cancel", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("switch", { name: /arm/i }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveAttribute("aria-live", "assertive");
    expect(screen.getByRole("button", { name: /cancel/i })).toHaveFocus();
  });

  it("links to the Cards reserve editor", () => {
    render(<ShoppingEditor shopping={policy} onChange={vi.fn()} />);
    expect(screen.getByRole("link", {name: /Manage Cards collection/})).toHaveAttribute("href", "/cards/");
  });

  it("round-trips a change to the visit frequency", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={policy} onChange={onChange} />);
    const field = screen.getByLabelText(/visit every n runs/i);
    fireEvent.change(field, { target: { value: "3" } });
    fireEvent.blur(field);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ visit_every_n_runs: 3 }),
    );
  });

  it("round-trips a change to the tap budget", () => {
    const onChange = vi.fn();
    render(<ShoppingEditor shopping={policy} onChange={onChange} />);
    const field = screen.getByLabelText(/max taps per visit/i);
    fireEvent.change(field, { target: { value: "80" } });
    fireEvent.blur(field);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ max_taps_per_visit: 80 }),
    );
  });

  it("says no visits happen at all when shopping is off, not that it rehearses", () => {
    render(<ShoppingEditor shopping={{ ...policy, enabled: false }} onChange={vi.fn()} />);
    expect(screen.getByText(/no shopping visits happen at all/i)).toBeInTheDocument();
    // ShoppingSession.begin() declines outright when `enabled` is false, so
    // no visit occurs - the rehearsal copy (navigates, reads, decides,
    // reports) would be a lie in this state and must not render.
    expect(screen.queryByText(/navigates to the shop/i)).toBeNull();
  });
});
it('summarizes existing Cards policy and links to its single editor',()=>{
 render(<ShoppingEditor shopping={policy} onChange={vi.fn()}/>);
 expect(screen.getByRole('link',{name:/Manage Cards collection/})).toHaveAttribute('href','/cards/');
 expect(screen.queryByLabelText('Gem floor')).not.toBeInTheDocument();
 expect(screen.getByText(/Reserve 40 gems/)).toBeInTheDocument();
});
