# Tinta4PlusU — Planned Improvements from Upstream PR #6

Source: https://github.com/joncox123/Tinta4Plus/pull/6

All four improvements have been implemented.

## Improvement 1: Brightness 0 = disable frontlight [DONE]

**What:** When `set-brightness` receives `level=0`, call `disable_frontlight()` instead of setting PWM to 0.

**Why:** On some panels, PWM 0 still produces a faint glow. Properly cutting the frontlight is more correct.

**Where:** `HelperDaemon.py`, in the `set-brightness` command handler.

**Change:** Add a check: if `level == 0`, call `ec.disable_frontlight()` and return. For non-zero levels, first call `ec.enable_frontlight()`, then `ec.set_brightness(level)`.

**Risk:** Minimal. Only affects the `level=0` edge case. No impact on other code paths.

**Test:** Set brightness to 0 via the GUI slider. Frontlight should fully turn off (no residual glow). Set it back to any non-zero value — frontlight should turn back on.

---

## Improvement 2: Display state verification + retry [DONE]

**What:** After applying a display switch via xrandr, read back the state and verify it matches expectations. If it doesn't, reset to native baseline and retry once.

**Why:** xrandr sometimes silently fails, leaving the display in a wrong resolution or scaling state. This catches and recovers from that.

**Where:** `DisplayManager.py`, add new methods:
- `_verify_display_target_state(display_name, target_state)` — reads back xrandr and compares framebuffer size + output geometry
- `_extract_actual_randr_state(display_name)` — parses current xrandr state
- `_reset_display_to_native_baseline(display_name)` — fallback reset to 1.0 scale before retry

Call verification after every `enable_display()` / `disable_display()` that uses xrandr (X11 path only, not Wayland/KDE).

**Risk:** Low. Verification is read-only. Retry only triggers if something already failed. Normal successful switches are unaffected.

**Test:**
1. Toggle to eInk — should work as before, verify resolution is correct.
2. Toggle back to OLED — same.
3. If possible, simulate a failure (e.g. wrong scale value) and confirm it recovers.

---

## Improvement 3: DPMS save/restore [DONE — in-memory, no file]

**What:** Before switching to eInk, capture the current DPMS timeout values. Disable DPMS during eInk mode. Restore original values when switching back to OLED.

**Why:** Currently we disable DPMS for eInk and force it on for OLED, but we don't preserve the user's original timeout settings. After switching back, screen timeout may differ from what it was.

**Where:** `Tinta4Plus.py` (or a new utility), using `xset q` to read and `xset dpms <standby> <suspend> <off>` to restore.

**Changes:**
- Before eInk switch: run `xset q`, parse the DPMS values, save to `~/.config/Tinta4PlusU/dpms_state`
- During eInk: `xset -dpms` (already done)
- On OLED switch-back: read saved file, run `xset dpms <values>` and `xset +dpms`

**Risk:** Low. Additive wrapper around existing DPMS logic. If save/restore fails, current behavior is unchanged.

**Test:**
1. Note your current DPMS settings (`xset q | grep -A2 "DPMS"`).
2. Switch to eInk — DPMS should be disabled.
3. Switch back to OLED — run `xset q` again, values should match step 1.

---

## Improvement 4: CLI toggle tool + Super+P hotkey [DONE]

**What:** A standalone `toggle-eink.py` script that toggles between OLED and eInk without the GUI.

**Why:** Allows binding a single keyboard shortcut (in DE settings or via a launcher) for instant display switching.

**Where:** New file `toggle-eink.py` in the project root. Reuses existing `HelperClient.py` and `DisplayManager.py`.

**Behavior:**
1. Connect to the helper daemon at `/tmp/tinta4plusu.sock`
2. Query current display state (which of eDP-1/eDP-2 is active)
3. If eInk is active: switch to OLED (with privacy image, frontlight off, theme restore)
4. If OLED is active: switch to eInk (enable eInk, set mode, optional frontlight)
5. Exit 0 on success, 1 on failure

**Risk:** None to existing code. This is a new standalone script. The GUI is completely untouched.

**Test:**
1. With GUI closed, run `python3 toggle-eink.py` from terminal — should switch to eInk.
2. Run again — should switch back to OLED.
3. Open GUI afterwards — should reflect the correct state.

---

## Skipped (too risky or not worth it now)

| Feature | Reason to skip |
|---------|---------------|
| Rotation/orientation | Touches DisplayManager deeply, needs testing across X11/Wayland/KDE |
| Touch input remapping | X11-only, we support Wayland — partial implementation would be confusing |
| HTTP-over-Unix-socket protocol | Complete IPC rewrite, no user-visible gain |
| Systemd socket activation | Different install model, limits portability |
| Event watcher (lid/randr) | Replaces keepalive which also carries global hotkey state |
| xrandr caching | Low reward, can add later |
