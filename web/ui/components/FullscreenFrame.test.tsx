import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { FullscreenFrame } from "./FullscreenFrame";
afterEach(() => {
 delete (HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen;
 delete (document as { exitFullscreen?: unknown }).exitFullscreen;
 Object.defineProperty(document, "fullscreenElement", { value: null, configurable: true });
});
test("fullscreen controls enter, exit and follow Escape", async () => {
 const set = (value: Element | null) => {
 Object.defineProperty(document, "fullscreenElement", { value, configurable: true });
 document.dispatchEvent(new Event("fullscreenchange"));
 };
 const request = vi.fn(function(this: HTMLElement) { set(this); return Promise.resolve(); });
 const exit = vi.fn(() => { set(null); return Promise.resolve(); });
 Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { value: request, configurable: true });
 Object.defineProperty(document, "exitFullscreen", { value: exit, configurable: true });
 render(<FullscreenFrame><span>Screen</span></FullscreenFrame>);
 await act(async () => fireEvent.click(screen.getByRole("button", { name: "Fullscreen" })));
 expect(request).toHaveBeenCalledOnce();
 await act(async () => fireEvent.click(screen.getByRole("button", { name: "Exit fullscreen" })));
 expect(exit).toHaveBeenCalledOnce();
 await act(async () => fireEvent.click(screen.getByRole("button", { name: "Fullscreen" })));
 act(() => set(null));
 expect(screen.getByRole("button", { name: "Fullscreen" })).toBeInTheDocument();
});
test("unsupported fullscreen uses a dismissible enlarged view", () => {
 render(<FullscreenFrame><span>Screen</span></FullscreenFrame>);
 fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
 expect(screen.getByRole("button", { name: "Exit fullscreen" })).toBeInTheDocument();
 fireEvent.keyDown(document, { key: "Escape" });
 expect(screen.getByRole("button", { name: "Fullscreen" })).toBeInTheDocument();
});
