import { expect, test } from "vitest";
import { ApiError } from "./api";
import { rerollCoordinatorUrl } from "./fleetRedirect";

test("worker reroll page moves to the main Fleet port", () => {
  expect(rerollCoordinatorUrl(
    new ApiError(503, "reroll_pool_unavailable"),
    "http://127.0.0.1:10020/fleet/reroll/",
  )).toBe("http://127.0.0.1:8765/fleet/reroll/");
});

test("main Fleet page and unrelated failures stay put", () => {
  const unavailable = new ApiError(503, "reroll_pool_unavailable");
  expect(rerollCoordinatorUrl(unavailable, "http://localhost:8765/fleet/reroll/"))
    .toBeNull();
  expect(rerollCoordinatorUrl(new ApiError(503, "other_error"),
    "http://localhost:10020/fleet/reroll/")).toBeNull();
  expect(rerollCoordinatorUrl(new Error("network failure"),
    "http://localhost:10020/fleet/reroll/")).toBeNull();
});
