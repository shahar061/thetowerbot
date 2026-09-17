import { expect, test, vi } from "vitest";
import FleetPage from "./page";

const redirect = vi.fn();
vi.mock("next/navigation", () => ({ redirect: (path: string) => redirect(path) }));

test("Fleet opens the manual Reroll pool", () => {
  FleetPage();
  expect(redirect).toHaveBeenCalledWith("/fleet/reroll/");
});
