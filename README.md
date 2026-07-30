# Fri3d Camp 2026 Badge — Hardware Self-Test

A single-screen MicroPythonOS app for the **Fri3d Camp 2026 badge** that checks whether
all hardware is working. Drop it into `/apps/` and launch it from the badge menu.

Authored by **David Steeman** — **Makerspace Baasrode**.

## What it does

On launch it shows a **~1 second splash** (app version, *David Steeman*, *Makerspace
Baasrode*), then the test screen: a 6×2 grid (320×240) with a **PASS / WARN / FAIL** status for each subsystem,
re-checked live. Inputs are tested interactively: tap the screen, press the buttons,
wiggle the joystick, point an IR remote at the receiver, and insert/remove the SD card.
Each of the 5 buttons colour-cycles its own NeoPixel (5 buttons ↔ 5 LEDs).

| Subsystem | How it's tested |
|---|---|
| Display | renders (always PASS) |
| IO Expander | reads CH32X035 firmware version over I²C |
| Touch (CST816S) | tap the screen (LVGL press event) |
| NeoPixel (×5) | `get_led_count()` + visual colour cycle |
| Battery | voltage in range (ADC) |
| IMU | I²C device `0x6A` present |
| Buttons | A / B / X / Y / **START** (rising-edge, latched) |
| Joystick | analog off-centre |
| microSD | live mount + file read (insert/remove reflected) |
| LoRa (SX1262) | real SPI comms: `begin()` → `getPacketType()` (no TX/RX) |
| Audio | buzzer output present |
| IR receiver | falling-edge interrupt on GPIO 11 |

Optional hardware (SD / LoRa / Audio / IR) reports **WARN** when absent rather than FAIL,
and none of them blocks the "all required hardware OK" summary.

## Requirements

- A Fri3d Camp 2026 badge running **MicroPythonOS** (built on MicroPython 1.27).
- `mpremote` (`pip install mpremote`) and membership of the `dialout` group.

## Install

```bash
# find the badge by stable id (the ttyACM* number shifts with plug order)
BADGE=$(readlink -f /dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_*-if00)

mpremote connect "$BADGE" cp -r org.fri3d.hwtest/ :/apps/
mpremote connect "$BADGE" exec "from mpos import AppManager; AppManager.refresh_apps()"
mpremote connect "$BADGE" exec "from mpos import AppManager; AppManager.restart_launcher()"
```

"Hardware Test" then appears in the launcher (alphabetical, around the H's — scroll if needed).
Tap it, or launch directly:

```bash
mpremote connect "$BADGE" exec "from mpos import AppManager; AppManager.start_app('org.fri3d.hwtest')"
```

## Using it

- **Tap** the screen → Touch flips to **TAP OK**.
- **Hold A / B / X / Y / S** → Buttons counts to **5/5**; each press colour-cycles that button's LED.
- **Wiggle the joystick** → Joystick shows direction, then **PASS**.
- **Point an IR remote** at the badge receiver and press → IR flips to **RX OK**.
- **Insert / remove the SD card** → microSD toggles between **OK** and **no card** within ~2 s.

The hint line turns green **"All required hardware OK"** once the required checks pass.
The app consumes the back/ESC (X) button so it stays open during testing — leave it by
resetting the badge.

## Layout

```
org.fri3d.hwtest/
├── MANIFEST.JSON      # app metadata + launcher intent filter
├── hwtest.py          # the self-test Activity
└── icon_64x64.png     # launcher icon
```

## License

MIT — see [LICENSE](LICENSE).
