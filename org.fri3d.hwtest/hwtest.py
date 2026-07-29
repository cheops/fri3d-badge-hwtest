# Hardware self-test for the Fri3d Camp 2026 badge.
# Shows a ~1 s startup splash (version / author / makerspace), then the
# single-screen (320x240) PASS / WARN / FAIL report.
# Live inputs: A/B/X/Y + START(S) buttons, joystick, screen tap, IR receiver,
# and microSD (insert/remove at any time).
# Each button push colour-cycles its NeoPixel (5 buttons <-> 5 LEDs).
import json
import logging
import lvgl as lv
import mpos
import machine
import os
from machine import Pin
from mpos import Activity, TaskManager, LightsManager, LoRaManager

logger = logging.getLogger(__name__)

# status colours
C_PASS = lv.color_hex(0x2DD36B)
C_FAIL = lv.color_hex(0xF4534A)
C_WARN = lv.color_hex(0xFFB300)
C_WAIT = lv.color_hex(0x9E9E9E)

FULLNAME = 'org.fri3d.hwtest'
SPLASH_MS = 1000   # how long the startup splash is shown

# face buttons read from mpos.io_expander.digital:
# (usb_plugged, joy_R, joy_L, joy_D, joy_U, MENU, B, A, Y, X, charger_stdby, charger_chg)
# (name, digital index, led index). START is on GPIO0, handled separately (led 4).
FACE = (('A', 7, 0), ('B', 6, 1), ('X', 9, 2), ('Y', 8, 3))

IR_RX_PIN = 11   # IR receiver data line (idle HIGH, pulses on a signal)
SD_MOUNT = '/sd'

# NeoPixel colour palette, cycled one step per press of the matching button
PAL = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 128, 0),
       (255, 0, 255), (0, 255, 255), (255, 255, 255), (0, 0, 0))
OFF = len(PAL) - 1  # index of black = LEDs off

# (label, key) in the order they wrap into the 2-column grid
CELLS = (
    ('Display', 'disp'), ('Buttons', 'btn'),
    ('IO Expander', 'exp'), ('Joystick', 'joy'),
    ('Touch', 'touch'), ('microSD', 'sd'),
    ('NeoPixel', 'led'), ('LoRa', 'lora'),
    ('Battery', 'batt'), ('Audio', 'audio'),
    ('IMU', 'imu'), ('IR', 'ir'),
)

# keys whose PASS is required for the "all good" summary
REQUIRED = ('disp', 'exp', 'touch', 'led', 'batt', 'imu', 'btn', 'joy')


def _read_version():
    """Read this app's version from its MANIFEST.JSON ('?' if not found)."""
    for base in ('/apps', '/builtin/apps'):
        try:
            f = open(base + '/' + FULLNAME + '/MANIFEST.JSON')
            v = json.load(f).get('version', '?')
            f.close()
            return v
        except Exception:
            pass
    return '?'


def _asset_bytes(name):
    """Read a binary asset from this app's folder (or builtin), or None."""
    for base in ('/apps', '/builtin/apps'):
        try:
            f = open(base + '/' + FULLNAME + '/' + name, 'rb')
            d = f.read(); f.close()
            return d
        except Exception:
            pass
    return None


def _joy_arrow(jx, jy):
    dx, dy = jx - 2048, 2048 - jy
    if abs(dx) < 400 and abs(dy) < 400:
        return 'center'
    if abs(dy) >= abs(dx):
        return 'up' if dy > 0 else 'down'
    return 'right' if dx > 0 else 'left'


class HwTest(Activity):

    def onCreate(self):
        self.im = mpos.InputManager()
        self.bm = mpos.BatteryManager()
        self.board = mpos.board.fri3d_2026
        self.start_pin = Pin(0, Pin.IN, Pin.PULL_UP)        # START button (S)
        self.ir_pin = Pin(IR_RX_PIN, Pin.IN, Pin.PULL_UP)   # IR receiver
        self.cells = {}
        self.ok = {}
        self.btn_seen = set()
        self.prev = {}
        self.led_state = [OFF, OFF, OFF, OFF, OFF]
        self.touch_tapped = False
        self.touch_present = False
        self.joy_done = False
        self._ir = bytearray(1)   # [0]=1 when an edge is seen on IR_RX
        self._ir_isr = None       # keeps the closure alive
        self._ir_ok = False
        self._sd_tick = 0
        self._task = None         # the live-update asyncio task
        self._entered = False     # has the self-test screen been shown?
        self._splash_task = None  # the splash->test timer task
        self.scr = None           # the self-test screen (built later)
        self.hint = None
        self._build_splash()

    # ---- splash / startup screen ----
    def _build_splash(self):
        sp = lv.obj()
        sp.set_style_pad_all(0, 0)
        sp.set_style_bg_color(lv.color_hex(0x141419), 0)
        sp.remove_flag(lv.obj.FLAG.SCROLLABLE)

        # David Steeman (top)
        who = lv.label(sp)
        who.set_text('David Steeman')
        who.align(lv.ALIGN.TOP_MID, 0, 44)
        who.set_style_text_color(lv.color_hex(0xFFFFFF), 0)

        # app version (below the name)
        ver = lv.label(sp)
        ver.set_text('v' + _read_version())
        ver.align(lv.ALIGN.TOP_MID, 0, 70)
        ver.set_style_text_color(C_WAIT, 0)

        # Makerspace Baasrode logo (bottom) -- makerspace.png asset (white tile
        # + black logo, rounded corners). Text fallback if the asset is missing.
        logo = _asset_bytes('makerspace.png')
        if logo:
            li = lv.image(sp)
            li.set_src(lv.image_dsc_t({'data_size': len(logo), 'data': logo}))
            li.align(lv.ALIGN.CENTER, 0, 30)
        else:
            org = lv.label(sp)
            org.set_text('Makerspace Baasrode')
            org.align(lv.ALIGN.BOTTOM_MID, 0, -28)
            org.set_style_text_color(C_PASS, 0)

        self.setContentView(sp)

    def _build_test(self):
        scr = lv.obj()
        scr.set_style_pad_all(2, 0)
        scr.set_style_bg_color(lv.color_hex(0x141419), 0)
        scr.remove_flag(lv.obj.FLAG.SCROLLABLE)             # every touch is a tap
        scr.add_event_cb(self._on_press, lv.EVENT.PRESSED, None)

        title = lv.label(scr)
        title.set_text('Hardware Self-Test')
        title.set_pos(4, 2)
        title.set_size(312, 18)
        title.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        title.set_style_text_color(lv.color_hex(0xE6E6E6), 0)

        grid = lv.obj(scr)
        grid.set_pos(2, 22)
        grid.set_size(316, 190)
        grid.set_style_pad_all(2, 0)
        grid.set_style_pad_gap(2, 0)
        grid.set_style_border_width(0, 0)
        grid.set_style_bg_opa(lv.OPA.TRANSP, 0)
        grid.set_flex_flow(lv.FLEX_FLOW.ROW_WRAP)
        grid.remove_flag(lv.obj.FLAG.CLICKABLE)
        grid.remove_flag(lv.obj.FLAG.SCROLLABLE)

        for label, key in CELLS:
            cell = lv.obj(grid)
            cell.set_size(154, 28)
            cell.set_style_pad_all(2, 0)
            cell.set_style_pad_gap(4, 0)
            cell.set_style_border_width(0, 0)
            cell.set_style_bg_opa(lv.OPA.TRANSP, 0)
            cell.set_flex_flow(lv.FLEX_FLOW.ROW)
            cell.set_flex_align(lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER)
            cell.remove_flag(lv.obj.FLAG.CLICKABLE)        # let taps fall through to screen
            nm = lv.label(cell)
            nm.set_text(label)
            nm.set_flex_grow(1)
            nm.set_style_text_color(lv.color_hex(0xCFCFD6), 0)
            st = lv.label(cell)
            st.set_text('--')
            st.set_style_text_color(C_WAIT, 0)
            self.cells[key] = st

        self.hint = lv.label(scr)
        self.hint.set_pos(4, 214)
        self.hint.set_size(312, 20)
        self.hint.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        self.hint.set_style_text_color(C_WAIT, 0)
        self.hint.set_text('Tap - A/B/X/Y/S - stick - point IR remote')
        self.scr = scr

    def _enter_test(self):
        if self._entered:
            return
        self._entered = True
        self._build_test()
        self.setContentView(self.scr)
        self.run_static_checks()
        self._render_leds()
        self._enable_ir()
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
        self._task = TaskManager.create_task(self._loop())

    async def _splash_then_enter(self):
        await TaskManager.sleep_ms(SPLASH_MS)
        self._enter_test()

    # ---- NeoPixel colour cycle (5 buttons <-> 5 LEDs) ----
    def _render_leds(self):
        try:
            for i in range(5):
                r, g, b = PAL[self.led_state[i]]
                LightsManager.set_led(i, r, g, b)
            LightsManager.write()
        except Exception:
            pass

    def _cycle_led(self, idx):
        self.led_state[idx] = (self.led_state[idx] + 1) % len(PAL)
        self._render_leds()

    # ---- IR receiver (edge interrupt; closure handler, no self in the ISR) ----
    def _enable_ir(self):
        try:
            flag = self._ir
            def isr(pin):
                flag[0] = 1
            self.ir_pin.irq(handler=isr, trigger=Pin.IRQ_FALLING)
            self._ir_isr = isr
        except Exception:
            pass

    def _disable_ir(self):
        try:
            self.ir_pin.irq(handler=None)
        except Exception:
            pass
        self._ir_isr = None

    # ---- tap (LVGL press event; cells/grid are non-clickable so it reaches here) ----
    def _mark_touch(self):
        if not self.touch_tapped:
            self.touch_tapped = True
            self.set_status('touch', 'TAP OK', C_PASS)
            self.ok['touch'] = True

    def _on_press(self, event):
        self._mark_touch()

    # ---- status helpers ----
    def set_status(self, key, text, color):
        st = self.cells.get(key)
        if st:
            st.set_text(text)
            st.set_style_text_color(color, 0)

    # ---- one-shot static (auto) checks ----
    def run_static_checks(self):
        self.set_status('disp', 'PASS', C_PASS); self.ok['disp'] = True

        try:
            v = mpos.io_expander.version
            self.set_status('exp', 'v' + '.'.join(map(str, v)), C_PASS); self.ok['exp'] = True
        except Exception:
            self.set_status('exp', 'FAIL', C_FAIL); self.ok['exp'] = False

        try:
            devs = self.board.i2c_devices
            self.touch_present = bool(self.im.has_pointer()) and (21 in devs)
        except Exception:
            self.touch_present = bool(self.im.has_pointer())
        if self.touch_present:
            self.set_status('touch', 'tap', C_WAIT)
        else:
            self.set_status('touch', 'FAIL', C_FAIL)
        self.ok['touch'] = False

        try:
            if LightsManager.is_available():
                n = LightsManager.get_led_count()
                if n == 5:
                    self.set_status('led', '5 OK', C_PASS); self.ok['led'] = True
                elif n > 0:
                    self.set_status('led', str(n) + '?', C_WARN); self.ok['led'] = False
                else:
                    self.set_status('led', '0', C_FAIL); self.ok['led'] = False
            else:
                self.set_status('led', 'NONE', C_FAIL); self.ok['led'] = False
        except Exception:
            self.set_status('led', 'FAIL', C_FAIL); self.ok['led'] = False

        try:
            if self.bm.has_battery():
                v = self.bm.read_battery_voltage()
                if 3.0 <= v <= 4.5:
                    self.set_status('batt', '%.2fV' % v, C_PASS); self.ok['batt'] = True
                else:
                    self.set_status('batt', '%.2fV?' % v, C_WARN); self.ok['batt'] = False
            else:
                self.set_status('batt', 'USB', C_WARN); self.ok['batt'] = False
        except Exception:
            self.set_status('batt', 'FAIL', C_FAIL); self.ok['batt'] = False

        try:
            imu_present = 106 in self.board.i2c_devices  # 0x6A
            self.set_status('imu', 'OK' if imu_present else 'FAIL', C_PASS if imu_present else C_FAIL)
            self.ok['imu'] = imu_present
        except Exception:
            self.set_status('imu', 'FAIL', C_FAIL); self.ok['imu'] = False

        # IR + SD are checked live (see update_live) so hot-plug / remote work
        self.set_status('ir', 'rx', C_WAIT); self.ok['ir'] = False
        self.set_status('sd', '...', C_WAIT); self.ok['sd'] = False

        # Audio (buzzer) - presence only
        try:
            present = self.board.buzzer_output is not None
            self.set_status('audio', 'OK' if present else 'none', C_PASS if present else C_WARN)
            self.ok['audio'] = present
        except Exception:
            self.set_status('audio', '?', C_WARN); self.ok['audio'] = False

        # LoRa (optional Seeed Studio Wio-SX1262-N): real SPI comms check, no TX/RX.
        # Configure LoRa mode with begin(), then read getPacketType(): 1=LoRa,
        # 0xFF = no chip answering (module not installed).
        try:
            sx = LoRaManager.radioChip
            if sx is None:
                self.set_status('lora', 'none', C_WARN); self.ok['lora'] = False
            else:
                try:
                    sx.standby()
                except Exception:
                    pass
                try:
                    sx.begin()  # set LoRa mode (no TX/RX); ignored if chip is in a
                                # bad state (ERR_WRONG_MODEM) -- we still read below
                except Exception:
                    pass
                pt = sx.getPacketType()
                if pt == 1:
                    self.set_status('lora', 'LoRa', C_PASS); self.ok['lora'] = True
                elif pt == 0:
                    self.set_status('lora', 'FSK', C_PASS); self.ok['lora'] = True
                else:
                    self.set_status('lora', 'no rsp', C_WARN); self.ok['lora'] = False
        except Exception:
            self.set_status('lora', 'err', C_WARN); self.ok['lora'] = False

    # ---- live (re-)checks: buttons, joystick, touch, IR, SD ----
    def _check_sd(self):
        # File-data reads are NOT cached (unlike a directory listing), so reading
        # a tiny probe file detects removal. Mount only when the read fails (first
        # insert / card was pulled). Never umount here -- on this badge that
        # contends the display-shared SPI bus and wedges the device.
        probe = SD_MOUNT + '/.hwtest'
        try:
            f = open(probe, 'r'); f.read(); f.close()
            self.set_status('sd', 'OK', C_PASS); self.ok['sd'] = True
            return
        except Exception:
            pass
        try:
            mpos.sdcard.mount(SD_MOUNT)
            try:
                f = open(probe, 'w'); f.write('ok'); f.close()
            except Exception:
                pass
            f = open(probe, 'r'); f.read(); f.close()
            self.set_status('sd', 'OK', C_PASS); self.ok['sd'] = True
        except Exception:
            self.set_status('sd', 'no card', C_WARN); self.ok['sd'] = False

    def update_live(self):
        # touch (backup path; primary path is the PRESSED event)
        if self.touch_present and not self.touch_tapped:
            try:
                x, y = self.im.pointer_xy()
                if x >= 0 and y >= 0:
                    self._mark_touch()
            except Exception:
                pass

        # IR receiver: any edge since last pass = a signal was seen
        if not self._ir_ok and self._ir[0]:
            self._ir_ok = True
            self.set_status('ir', 'RX OK', C_PASS)
            self.ok['ir'] = True
            self._disable_ir()

        # microSD: re-check ~every 2 s so insert/remove is reflected
        self._sd_tick = (self._sd_tick + 1) % 100
        if self._sd_tick % 20 == 0:
            self._check_sd()

        # read current button states
        pressed = {}
        try:
            d = mpos.io_expander.digital
            for name, idx, _led in FACE:
                pressed[name] = bool(d[idx])
        except Exception:
            pass
        try:
            pressed['S'] = (self.start_pin.value() == 0)
        except Exception:
            pass

        # rising edge -> mark seen + colour-cycle that button's LED
        for name, _idx, led in FACE:
            now = pressed.get(name, False)
            if now and not self.prev.get(name, False):
                self.btn_seen.add(name)
                self._cycle_led(led)
            self.prev[name] = now
        nows = pressed.get('S', False)
        if nows and not self.prev.get('S', False):
            self.btn_seen.add('S')
            self._cycle_led(4)
        self.prev['S'] = nows

        nb = len(self.btn_seen)
        self.set_status('btn', '%d/5' % nb, C_PASS if nb >= 5 else C_WAIT)
        self.ok['btn'] = nb >= 5

        # joystick
        try:
            a = mpos.io_expander.analog
            moved = abs(a[3] - 2048) > 400 or abs(a[4] - 2048) > 400
            if moved:
                self.joy_done = True
            if self.joy_done:
                self.set_status('joy', 'PASS', C_PASS)
            else:
                self.set_status('joy', _joy_arrow(a[4], a[3]), C_WAIT)
            self.ok['joy'] = self.joy_done
        except Exception:
            self.set_status('joy', '?', C_WARN); self.ok['joy'] = False

    def update_summary(self):
        if all(self.ok.get(k) for k in REQUIRED):
            self.hint.set_text('All required hardware OK')
            self.hint.set_style_text_color(C_PASS, 0)
        else:
            self.hint.set_text('Tap - A/B/X/Y/S - stick - point IR remote')
            self.hint.set_style_text_color(C_WAIT, 0)

    async def _loop(self):
        while True:
            try:
                self.update_live()
            except Exception:
                pass
            try:
                self.update_summary()
            except Exception:
                pass
            await TaskManager.sleep_ms(100)

    # ---- lifecycle ----
    def onBackPressed(self, screen):
        # Consume back / ESC (the X button) so the test app stays foreground and
        # every button can be exercised. Leave by resetting the badge.
        return True

    def onResume(self, screen):
        super().onResume(screen)
        if not self._entered:
            # first start: show the splash, then switch to the self-test
            if self._splash_task is None:
                self._splash_task = TaskManager.create_task(self._splash_then_enter())
        else:
            # returning to an already-started test: just resume polling
            self._enable_ir()
            if self._task is None:
                self._task = TaskManager.create_task(self._loop())

    def onPause(self, screen):
        if self._splash_task is not None:
            try:
                self._splash_task.cancel()
            except Exception:
                pass
            self._splash_task = None
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self._disable_ir()
        try:
            self.led_state = [OFF, OFF, OFF, OFF, OFF]
            self._render_leds()
        except Exception:
            pass
        super().onPause(screen)
