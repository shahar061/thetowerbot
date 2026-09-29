/** Width over height of the emulator screen (EXPECTED_RESOLUTION, 1080x2400 portrait). */
export const SCREEN_ASPECT = 1080 / 2400;
/** Space between tiles, matching the wall's `gap-2`. */
export const WALL_GAP_PX = 8;
/** Height of the name strip above each screen, matching its `h-7`. */
export const TILE_HEADER_PX = 28;

/** The column count that gives every screen the most room in a `width` x `height` area. */
export function wallLayout(count: number, width: number, height: number): { columns: number; tileWidth: number } {
  let best = { columns: 0, tileWidth: 0 };
  for (let columns = 1; columns <= count; columns++) {
    const rows = Math.ceil(count / columns);
    const byWidth = (width - (columns - 1) * WALL_GAP_PX) / columns;
    const byHeight = ((height - (rows - 1) * WALL_GAP_PX) / rows - TILE_HEADER_PX) * SCREEN_ASPECT;
    const tileWidth = Math.max(0, Math.min(byWidth, byHeight));
    if (tileWidth > best.tileWidth || !best.columns) best = { columns, tileWidth };
  }
  return best;
}
