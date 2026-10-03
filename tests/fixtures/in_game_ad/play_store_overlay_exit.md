# Google Play product-sheet ad exit

Captured on BlueStacks Air 83 at 1080×2400. The recording starts after the
Castle Busters ad had already opened its Google Play product sheet; the earlier
video creative was no longer available. `play_store_overlay_exit_83.mp4`
records the sheet, its dismissal, The Tower reloading, and the first return
dialog. The three JPG frames preserve the product sheet and both return
dialogs at native resolution.

Observed sequence:

1. Foreground window: `com.android.vending/com.google.android.finsky.transparentmainactivity.HsdpAlias`.
   The sheet's X is centered at approximately (1000, 940).
2. Closing the sheet returns focus to The Tower, which reloads and asks
   **Resume previous round?** The **Resume** button is centered at (722, 1425).
3. The resumed battle shows the **Cloud save is now available** prompt.
   **Maybe later** is centered at (539, 1638).
4. After dismissal, the battle continued at Tier 1, Wave 88. The ad reward
   cannot be inferred from this return sequence; it remains uncertain unless
   the normal balance check confirms the exact gem increase.

The exit reader requires the exact foreground activity and witnessed visual
or OCR controls. It does not infer a close point from the ad artwork or tap
the **End run**, **Create Account**, or **Install** buttons.
