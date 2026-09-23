import { render, screen } from "@testing-library/react";
import { describe as group, expect, it } from "vitest";
import { CurrencyAmount, signed } from "./CurrencyAmount";

group("CurrencyAmount", () => {
  it("shows an unread amount as unknown, never as zero", () => {
    render(<CurrencyAmount currency="coins" delta={null} />);

    expect(screen.getByText("?")).toBeDefined();
    expect(screen.getByTitle("unknown amount of coins")).toBeDefined();
  });

  it("shows provably-nothing as 0, not as unknown", () => {
    render(<CurrencyAmount currency="gems" delta={0} />);

    expect(screen.getByText("0")).toBeDefined();
    expect(screen.queryByText("?")).toBeNull();
  });

  it("names its currency for screen readers, not only with a shape", () => {
    render(<CurrencyAmount currency="stones" delta={10} />);

    expect(screen.getByText("stones")).toBeDefined();
  });

  it("draws a marker even for a currency it has no shape for", () => {
    const { container } = render(<CurrencyAmount currency="resource:foo@1" delta={3} />);

    expect(container.querySelector("svg")).not.toBeNull();
  });
});

group("signed", () => {
  it("separates thousands and keeps the sign", () => {
    expect(signed(1840)).toBe("+1,840");
    expect(signed(-900)).toBe("-900");
    expect(signed(0)).toBe("0");
  });
});
