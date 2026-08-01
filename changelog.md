# Changelog

## 2026-08-01 — LoRa hardware reset via CH32 expander (v0.5.1 → v0.5.2)

The 2026-07-30 session (below) concluded the LoRa chip had **no working reset line** and
built the probe around that limitation (single shot, no retries, "???" on ambiguous
reads). That turned out to be wrong: `lucid-void/fri3d-meshcore`
[issue #7](https://github.com/lucid-void/fri3d-meshcore/issues/7) found the SX1262's
reset pin isn't absent — it's wired through the CH32 I/O-expander (the same chip behind
`mpos.io_expander`) rather than to the ESP32-S3 directly, and is reachable by toggling
the expander's `config` register: `0x03` holds LoRa in reset (LCD/aux stay powered),
`0x13` releases it.

Ported that mechanism into `_reset_lora_via_ch32()` in `hwtest.py`. `_probe_lora()` now
resets the chip before its first `standby()` attempt (was already doing a settle delay
for the same boot-timing issue, so this piggybacks on it) and, if that read isn't clean,
resets again and retries once — something the previous version explicitly avoided doing
because a bad transaction used to wedge the chip for the rest of the session with no way
back short of a power-cycle. That's no longer true with a real reset available, so the
retry is now worth doing. Updated the stale "no working reset line" comments in
`hwtest.py` and the LoRa note in `README.md` accordingly.

Bumped `org.fri3d.hwtest` to **0.5.2**, rebuilt the `.mpk`.

### Flashed and tested on hardware (Devbac8, MAC `90:70:69:01:ba:c8`) — result: unverified

Flashed 0.5.2 to Devbac8 (the other badge stayed untouched, mid-use for unrelated
development). Instrumented a throwaway debug build (device-only, never committed) to log
every `exp.config` write and readback, then inspected it after launching the app by
reaching the live `HwTest` instance through `mpos.activity_navigator.screen_stack` and
pulling `LORA_DEBUG` out of `onCreate.__globals__` (no module-registry entry exists for
app entrypoints — they're `exec()`'d into their own namespace, not `import`ed).

**Finding: the CH32 `config` register write appears to be a no-op on this hardware.**
Reading `exp.config` back immediately after writing `0x03` (and again after `0x13`)
showed no change at all from whatever it read *before* the write — both times it stayed
at the board's boot-time default (`0x13`). A control test against a different expander
register, `lcd_brightness`, wrote and read back correctly on the first try, so the I2C
write path itself works — this is specific to `config`. Whether the CH32 firmware
(v2.0.1, confirmed via `exp.version`) actually pulses the reset pin and just doesn't
reflect it in the readback, or genuinely ignores runtime writes to that register after
boot-time init, isn't something I can tell from software alone.

Net effect: this can't be reported as a confirmed fix. Kept the code (it's a correct
port of meshcore's approach, matches the bit layout in MicroPythonOS's own
`board/fri3d_2026.py`, and is harmless even if inert), but reworded the `hwtest.py`
comments and README to say the reset's effect is unverified rather than claiming it
resolves the hang. The LoRa probe's OK/???  result kept varying run-to-run in exactly the
pattern already described in the pre-existing shared-SPI-bus-timing comments, independent
of whether the reset call did anything.

Also hit an unrelated scare mid-session: three `mpos.AppManager.start_app()` calls fired
back-to-back (~5s apart) from overlapping `mpremote exec` sessions left the badge fully
unresponsive on USB serial (no REPL, no data, even after 20s+). A physical power-cycle
(by the user) recovered it, and a single clean launch afterward behaved normally with no
recurrence — most likely overlapping `mpremote` sessions racing for the raw-REPL/USB
console rather than anything in the LoRa/CH32 code, but noted here since it looked
alarming in the moment.

Filed upstream:
[MicroPythonOS/MicroPythonOS#224](https://github.com/MicroPythonOS/MicroPythonOS/issues/224).

### Next steps (not done this session)
- Confirm with a scope/logic analyzer whether the CH32 physically pulses the LoRa reset
  pin on a `config` write, since software alone can't distinguish "no-op" from "pulses
  too fast/self-clears before readback."
- Check whether `lucid-void/fri3d-meshcore` verified their own reset call actually takes
  effect, or hit the same unverifiable-readback situation and it just didn't matter for
  their use case.
- Worth asking whether a newer CH32 firmware than v2.0.1 is expected to support runtime
  `config` writes, if v2.0.1 turns out to genuinely ignore them.
- Watch [MicroPythonOS#224](https://github.com/MicroPythonOS/MicroPythonOS/issues/224)
  for a maintainer response.

## 2026-07-30 — Ship the "fix neopixels" PR (v0.4.1 → v0.5.1)

### Goal

Merge the community "fix neopixels" PR and get it into production: test on live badges,
package a new `.mpk`, and get it ready for BadgeHub. Along the way this turned into a much
larger pass — several real bugs (some pre-existing, some introduced by the PR) surfaced
under live-hardware testing and got fixed.

### 1. Merged PR + firmware compatibility (v0.4.1)

- Pulled the merged "fix neopixels" PR (#1, by `cheops`), which switched NeoPixel control
  from `mpos.lights.*` to the newer `LightsManager` framework.
- Discovered `LightsManager` didn't exist on the firmware the test badges were running
  (0.14.2 / 0.15.1) — only the older `mpos.lights` module was available, so the app
  crashed with `ImportError` on launch.
- OTA-updated both 2026 badges to MicroPythonOS **0.16.0** (latest release) via the
  built-in `com.micropythonos.osupdate` app, which has `LightsManager`.
- Bumped `org.fri3d.hwtest` to 0.4.1, confirmed NeoPixel test now reports "5 OK" on both
  badges.

### 2. LoRa check: root cause + upstream fix (v0.4.2 → later reworked)

The LoRa presence check (`standby()` → `begin()` → `getPacketType()`) intermittently
reported "no rsp" even with a module physically installed, and could occasionally hang
the whole badge requiring a **physical power-cycle** to recover (no software reset works
— see below).

**Root cause** (traced into `MicroPythonOS`'s own source, both the app-level driver and
the `lvgl_micropython` submodule):

- The Fri3d 2026 board's display and LoRa chip **share one physical SPI bus**
  (`board/fri3d_2026.py`). The LoRa device uses `cs=-1` (software-managed chip-select via
  a plain GPIO), not the hardware CS the SPI framework manages.
- `drivers/lora/sx1262.py`'s `SPItransfer()` asserts CS low, *then* busy-waits for the
  BUSY pin (up to 5s, twice per command) before sending anything — holding the shared bus
  hostage for that whole span with nothing coordinating against the display's own SPI
  traffic.
- ESP-IDF's `spi_device_acquire_bus()`/`release_bus()` (in `lvgl_micropython`'s
  `machine_hw_spi.c`) already exist and are used — but only per individual
  `machine.SPI` call, not across the whole CS-low span.
- The board also has **no working reset line** for the LoRa chip (board init passes the
  IR-receiver pin as a placeholder) — so a corrupted transaction can leave the chip
  unresponsive for the rest of the session with no software recovery.

Filed upstream: **[MicroPythonOS/MicroPythonOS#222](https://github.com/MicroPythonOS/MicroPythonOS/pull/222)**
— reorders the busy-wait to happen *before* CS is asserted (shrinks the risk window from
"up to 5–10s idle CS-low" down to just the transfer time), plus a writeup proposing a
`lock()`/`unlock()` API on `machine.SPI.Device` as the real fix.

App-side mitigation history (each step tested live on hardware):
1. Retry loop, 3 attempts, 700ms apart, first attempt delayed 3s — still ~1-in-5 false
   negatives, and discovered each failed attempt could itself block for many seconds
   (driver's busy-wait), so retrying is not "independent trials" — once the first probe
   corrupts the chip state, later retries are usually doomed too (no reset line).
2. Bumped to 5 attempts — didn't meaningfully help, confirmed the "coin flip on the first
   probe" theory.
3. **Final approach (v0.5.1):** single minimal `standby()` probe only (not the full
   3-command sequence), run in `onCreate()` before this app builds any screen of its own
   — the quietest moment available. Even so, confirmed on hardware: opening the app right
   after a reset reads "no coherent response" **every time** on a badge with a module
   installed, and "OK" on every later reopen in the same boot session (tied to the
   launcher's one-time-only expensive icon-grid build on its very first `onResume()`).
   A boot-aware longer settle delay (`LORA_BOOT_SETTLE_MS`/`LORA_BOOT_GRACE_MS`) reduces
   but does not eliminate this.
   **Decision: stop chasing it.** Negative reads now show **`???`** instead of a
   confident-looking `no rsp`, since this app cannot fully distinguish "module missing"
   from "hit this known timing issue."

### 3. Button/NeoPixel crash fix

Pressing multiple buttons in the same ~100ms poll tick called `LightsManager.write()`
once per button, back-to-back with zero gap — reported by the user as buttons not
lighting LEDs and the badge rebooting. Fixed by batching: update `led_state` for every
button that changed this tick, then issue a single `write()` at the end. Confirmed fixed
on hardware (4/4 clean runs after the fix, vs. reproducible before it).

### 4. Double-click X to quit

The app previously consumed the back/ESC (X) button unconditionally (comment: "leave by
resetting the badge"). Added: single press consumed (X stays testable), quick
double-press quits.

First implementation had a bug: `onCreate()`'s splash screen and `_enter_test()`'s test
screen each call `setContentView()`, and **every** `setContentView()` call pushes a new
entry onto the global activity stack (`mpos/ui/view.py`) — so once the test screen is
showing, this app occupies *two* stack layers. A single `finish()` only popped one,
revealing the app's own earlier splash screen instead of the launcher (needed a second
double-click to actually exit). Fixed by popping both of the app's own layers when it has
gotten that far (checked via `self._entered`).

### 5. Launcher icon overlap

Renaming the app to "Hardware Test" (from the abbreviated "HW Test") made its launcher
label wrap to two lines — the launcher's grid layout (`launcher.py`) hardcodes
`label_height = 24` sized for one line; a real 2-line label needs 32px, so it overflows
~8-10px upward into the icon's nominal 64×64 box (confirmed by measuring live widget
heights on-device — this affects other multi-word app names too, e.g. "File Manager").
Not fixable from this app (launcher.py is OS code) — worked around by:
- Making the icon's background **transparent** (chroma-keyed the original's pure-black
  background — confirmed via pixel analysis it was cleanly confined to the background,
  not used inside the glyph) instead of opaque black.
- Shrinking the artwork to ~78% and **top-aligning** it (all the freed margin goes to the
  bottom, where the real intrusion happens).

Gotcha hit while verifying this: the launcher caches its *own rendered UI*
(`_last_app_list` in `launcher.py`), separately from `AppManager`'s app data —
`AppManager.refresh_apps()` alone does **not** make it re-read a changed icon file; needed
`AppManager.restart_launcher()` to force a full rebuild. A real end-user update (reboot
after installing) won't hit this, since the launcher's cache starts empty on every boot.

### 6. Renamed app: "HW Test" → "Hardware Test"

Changed `name` in both `metadata.json` and `MANIFEST.JSON`, and the one README reference.

### Version history this session

| Version | Change |
|---|---|
| 0.4.1 | Merged neopixel fix, `LightsManager` compat |
| 0.4.2 | LoRa retry (3 attempts) |
| 0.5.0 | LoRa retry bumped to 5 attempts, app rename, double-click quit, icon fix (v1) |
| 0.5.1 | LoRa reworked to single early probe + "???" wording, icon fix (v2, actually visible), quit-stack fix |

### BadgeHub publishing

- `.mpk` rebuilt at each version bump; `org.fri3d.hwtest-0.5.1.mpk` is the current
  publish-ready package.
- Established convention (per user, 2026-07-30) for staging BadgeHub publish files: full
  layout under `/storage/fileshare/<fullname>/` — the `.mpk`, a `.mpk.zip` duplicate, a
  standalone `icon_64x64.png` + `metadata.json`, and an extracted `<fullname>/` subfolder
  with the `.mpk`'s contents. Saved as a memory (`badgehub_publish_layout`) so this is
  done automatically next time without being asked.
- Actual upload to badgehub.eu is a manual step (needs the user's login) — not done by
  Claude.

### Firmware: full-erase reflash of both badges

Near the end of the session, one badge was crashing and the other reported "not enough
space" installing the app. Full-erase reflashed both with the same
`MicroPythonOS_esp32s3_0.16.0.bin` release build:

```bash
esptool --chip esp32s3 --port <device> write-flash --erase-all 0 MicroPythonOS_esp32s3_0.16.0.bin
```

Notes:
- esptool's automatic reset-into-bootloader didn't work over these badges' native
  USB-Serial/JTAG interface — needed the user to manually hold BOOT while
  reconnecting/resetting.
- In bootloader mode, badges enumerate under a different `/dev/serial/by-id` name
  (`usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`) than their normal running-OS
  name (`usb-Espressif_Systems_Espressif_Device_<serial>-if00`) — expected, per the
  by-id-not-ttyACM convention, but worth knowing the name changes with mode.
- Badge 1 took unusually long to come back up after the erase+flash (several minutes,
  no boot log output) — user resolved it themselves (reflashed manually, reinstalled the
  app) before it was fully diagnosed. Not confirmed whether this was just a slow
  first-boot filesystem format or something else.

### Known issues / follow-up

- **LoRa false-negative is not solved**, only made honest (`???`) and less frequent
  (single early probe). Real fix needs either the upstream driver change (PR #222) to
  land and ship in a new MicroPythonOS release, or a from-scratch look at avoiding the
  shared bus entirely.
- PR #222 is unmerged and not hardware-verified end-to-end (the reorder fix itself is a
  small, low-risk pure-Python change I'm confident in by inspection, but repeated heavy
  testing this session progressively made the test badges' LoRa chips unreliable, so a
  clean before/after comparison wasn't obtained in-session).
- BadgeHub upload for v0.5.1 still needs to be done manually by the user.
