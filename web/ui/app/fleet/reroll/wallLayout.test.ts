import { describe as group, expect, it } from "vitest";
import { SCREEN_ASPECT, TILE_HEADER_PX, WALL_GAP_PX, wallLayout } from "./wallLayout";

group("wallLayout", () => {
  it("puts four portrait screens in one row on a 16:9 monitor", () => {
    const layout = wallLayout(4, 1920, 1080);
    expect(layout.columns).toBe(4);
    expect(layout.tileWidth).toBeCloseTo((1080 - TILE_HEADER_PX) * SCREEN_ASPECT);
  });

  it("wraps into rows when a single row would make the screens smaller", () => {
    expect(wallLayout(12, 1920, 1080).columns).toBe(6);
    expect(wallLayout(6, 800, 1200).columns).toBe(3);
  });

  it("caps a lone screen by the window height", () => {
    expect(wallLayout(1, 1920, 1080)).toEqual({ columns: 1, tileWidth: (1080 - TILE_HEADER_PX) * SCREEN_ASPECT });
  });

  it("caps screens by the window width when the window is narrow", () => {
    expect(wallLayout(2, 600, 1080).tileWidth).toBeCloseTo((600 - WALL_GAP_PX) / 2);
  });

  it("has nothing to lay out without screens or space", () => {
    expect(wallLayout(0, 1920, 1080)).toEqual({ columns: 0, tileWidth: 0 });
    expect(wallLayout(3, 0, 0).tileWidth).toBe(0);
  });
});
