# GeekToken v2.2.3 — Pico 2 W TOTP + GT Protocol + OTA + LED
# GT Protocol: JSON lines over USB serial
# Wiring: SDA→GP4  SCL→GP5  VCC→3V3  GND→GND  LED→GP1

VERSION      = "2.2.4"
GITHUB_USER  = "Ilikehomeassistant"
GITHUB_REPO  = "GeekToken"
VERSION_URL  = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/firmware/version.json"
FIRMWARE_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/firmware/main.py"

import uasyncio as asyncio
import json, sys, time, struct, hashlib, select, network, ntptime, framebuf
from machine import I2C, Pin, PWM, reset

# ═══════════════════════════════════════════════════════════════
#  LED (GP1) — breathing heartbeat pattern
# ═══════════════════════════════════════════════════════════════

_led_pwm  = PWM(Pin(1))
_led_pwm.freq(1000)
_led_mode = "heartbeat"   # heartbeat | blink | fast | off | on

async def led_task():
    import math
    t = 0
    while True:
        if _led_mode == "heartbeat":
            # Double-pulse heartbeat: two quick beats then pause
            # Beat 1
            for i in range(0, 65536, 3000):
                _led_pwm.duty_u16(i); await asyncio.sleep_ms(10)
            for i in range(65535, 0, -3000):
                _led_pwm.duty_u16(i); await asyncio.sleep_ms(10)
            await asyncio.sleep_ms(80)
            # Beat 2
            for i in range(0, 40000, 3000):
                _led_pwm.duty_u16(i); await asyncio.sleep_ms(10)
            for i in range(40000, 0, -3000):
                _led_pwm.duty_u16(i); await asyncio.sleep_ms(10)
            await asyncio.sleep_ms(700)

        elif _led_mode == "blink":
            _led_pwm.duty_u16(65535)
            await asyncio.sleep_ms(100)
            _led_pwm.duty_u16(0)
            await asyncio.sleep_ms(900)

        elif _led_mode == "fast":
            _led_pwm.duty_u16(65535)
            await asyncio.sleep_ms(50)
            _led_pwm.duty_u16(0)
            await asyncio.sleep_ms(50)

        elif _led_mode == "on":
            _led_pwm.duty_u16(65535)
            await asyncio.sleep_ms(100)

        elif _led_mode == "off":
            _led_pwm.duty_u16(0)
            await asyncio.sleep_ms(100)

        else:
            await asyncio.sleep_ms(50)

def set_led(mode):
    global _led_mode
    _led_mode = mode

# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

def load_cfg():
    try:
        with open('config.json') as f: return json.load(f)
    except:
        return {'wifi': {'ssid': 'SKYF30B4', 'pass': 'PWCSDPFU'}, 'accounts': []}

def save_cfg(c):
    with open('config.json', 'w') as f: json.dump(c, f)

# ═══════════════════════════════════════════════════════════════
#  SSD1306 (128x32)
# ═══════════════════════════════════════════════════════════════

class SSD1306_I2C(framebuf.FrameBuffer):
    def __init__(self, i2c, addr=0x3c):
        self.i2c = i2c; self.addr = addr
        self.buf = bytearray(128 * 4)
        super().__init__(self.buf, 128, 32, framebuf.MONO_VLSB)
        self._cmd(0xae,0x20,0x00,0x40,0xa1,0xa8,0x1f,
                  0xc8,0xd3,0x00,0xda,0x02,0xd5,0x80,
                  0xd9,0xf1,0xdb,0x30,0x81,0xff,0xa4,
                  0xa6,0x8d,0x14,0xaf)
        self.fill(0); self.show()

    def _cmd(self, *cmds):
        for c in cmds: self.i2c.writeto(self.addr, bytes([0x80, c]))

    def show(self):
        self._cmd(0x21,0,127,0x22,0,3)
        self.i2c.writeto(self.addr, b'\x40' + self.buf)

# ═══════════════════════════════════════════════════════════════
#  HMAC-SHA1 / base32 / TOTP
# ═══════════════════════════════════════════════════════════════

def hmac_sha1(key, msg):
    if len(key) > 64: key = hashlib.sha1(key).digest()
    key = key + b'\x00' * (64 - len(key))
    o = bytes(b ^ 0x5c for b in key)
    i = bytes(b ^ 0x36 for b in key)
    return hashlib.sha1(o + hashlib.sha1(i + msg).digest()).digest()

_B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
def b32dec(s):
    s = s.upper().replace(' ','').rstrip('=')
    bits, buf, out = 0, 0, []
    for c in s:
        buf = (buf << 5) | _B32.index(c)
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((buf >> bits) & 0xff)
    return bytes(out)

def totp(secret):
    key = b32dec(secret)
    t   = time.time() // 30
    h   = hmac_sha1(key, struct.pack('>Q', t))
    off = h[19] & 0xf
    n   = ((h[off]   & 0x7f) << 24 | (h[off+1] & 0xff) << 16 |
           (h[off+2] & 0xff) << 8  |  h[off+3] & 0xff)
    return n % 1_000_000

def remaining():
    return 30 - (time.time() % 30)

# ═══════════════════════════════════════════════════════════════
#  Display helpers
# ═══════════════════════════════════════════════════════════════

def draw_large(oled, text, x, y):
    tmp = framebuf.FrameBuffer(bytearray(8), 8, 8, framebuf.MONO_VLSB)
    for idx, ch in enumerate(text):
        tmp.fill(0); tmp.text(ch, 0, 0, 1)
        for r in range(8):
            for col in range(8):
                if tmp.pixel(col, r):
                    oled.fill_rect(x + idx*14 + col*2, y + r*2, 2, 2, 1)

def show_code(oled, code, secs, label):
    oled.fill(0)
    oled.text(label, max(0,(128-len(label)*8)//2), 0, 1)
    s = "{:06d}".format(code)
    disp = s[:3] + " " + s[3:]
    draw_large(oled, disp, max(0,(128-len(disp)*14)//2), 10)
    # bar drains as time runs out
    filled = int((secs / 30) * 124)
    oled.fill_rect(2, 29, 124, 3, 0)
    oled.fill_rect(2, 29, filled, 3, 1)
    if secs <= 5: oled.rect(0, 28, 128, 4, 1)
    oled.show()

def show_msg(oled, l1, l2='', l3=''):
    oled.fill(0)
    oled.text(l1, 0, 0, 1)
    if l2: oled.text(l2, 0, 12, 1)
    if l3: oled.text(l3, 0, 24, 1)
    oled.show()

# ═══════════════════════════════════════════════════════════════
#  WiFi helper
# ═══════════════════════════════════════════════════════════════

def wifi_connect(ssid, pwd, timeout=20):
    global wifi_ok
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, pwd)
        for _ in range(timeout):
            if wlan.isconnected(): break
            time.sleep(1)
    wifi_ok = wlan.isconnected()
    return wlan

# ═══════════════════════════════════════════════════════════════
#  OTA Update
# ═══════════════════════════════════════════════════════════════

def check_ota(oled):
    import urequests
    try:
        info = None
        for attempt in range(4):
            try:
                show_msg(oled, "Checking OTA...", f"attempt {attempt+1}")
                set_led("fast")
                r = urequests.get(VERSION_URL, timeout=15)
                info = r.json(); r.close()
                if info.get('version'): break
            except Exception as e:
                show_msg(oled, f"retry {attempt+1}/4", str(e)[:16])
                time.sleep(3)
        if not info or not info.get('version'):
            show_msg(oled, "OTA check fail", "no version data")
            set_led("heartbeat")
            time.sleep(2)
            return False
        latest = info.get('version', VERSION)
        notes  = info.get('notes', '')

        if latest == VERSION:
            show_msg(oled, "Up to date!", f"v{VERSION}")
            set_led("heartbeat")
            time.sleep(2)
            return False

        # New version available
        show_msg(oled, "Update found!", f"v{VERSION}->v{latest}", notes[:16])
        time.sleep(2)
        show_msg(oled, "Downloading...", f"v{latest}")

        r = urequests.get(FIRMWARE_URL, timeout=60)
        written = 0
        with open('main_ota.py', 'wb') as f:
            while True:
                chunk = r.raw.read(512)
                if not chunk: break
                f.write(chunk)
                written += len(chunk)
        r.close()

        if written < 5000:
            show_msg(oled, "DL too small!", f"{written}b - abort")
            set_led("heartbeat")
            time.sleep(3)
            return False

        import uos
        try:    uos.remove('main.py')
        except: pass
        uos.rename('main_ota.py', 'main.py')

        show_msg(oled, "OTA complete!", f"v{latest}", "Rebooting...")
        set_led("on")
        time.sleep(2)
        reset()
        return True

    except Exception as e:
        show_msg(oled, "OTA failed", str(e)[:16])
        set_led("heartbeat")
        time.sleep(2)
        return False

# ═══════════════════════════════════════════════════════════════
#  Global state
# ═══════════════════════════════════════════════════════════════

cfg         = load_cfg()
oled_dev    = None
cur_acc     = 0
time_synced = False
wifi_ok     = False
start_ms    = time.ticks_ms()
_upd_buf    = []
_upd_mode   = False

# ═══════════════════════════════════════════════════════════════
#  WiFi + NTP task
# ═══════════════════════════════════════════════════════════════

async def do_wifi_ntp(check_update=False):
    global time_synced, wifi_ok
    ssid = cfg['wifi'].get('ssid','')
    pwd  = cfg['wifi'].get('pass','')
    if not ssid:
        show_msg(oled_dev, "No WiFi set", "Use Manager app")
        await asyncio.sleep(2); return

    set_led("fast")
    show_msg(oled_dev, "Connecting...", ssid[:16])
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, pwd)

    for i in range(20):
        if wlan.isconnected(): wifi_ok = True; break
        show_msg(oled_dev, "Connecting"+"."*(i%4), ssid[:16])
        await asyncio.sleep(1)

    if wifi_ok:
        set_led("blink")
        await asyncio.sleep(2)
        show_msg(oled_dev, "Syncing time...")
        try:
            ntptime.settime(); time_synced = True
        except:
            pass

        if check_update:
            check_ota(oled_dev)

        show_msg(oled_dev, "GeekToken v2.1", "WiFi OK" if wifi_ok else "No WiFi",
                 "Time OK" if time_synced else "No time sync")
        await asyncio.sleep(1)
    else:
        show_msg(oled_dev, "WiFi failed!", ssid[:16])
        await asyncio.sleep(2)

    wlan.disconnect(); wlan.active(False)
    set_led("heartbeat")

# ═══════════════════════════════════════════════════════════════
#  GT Protocol
# ═══════════════════════════════════════════════════════════════

def gt_send(obj):
    sys.stdout.write(json.dumps(obj) + '\n')

async def gt_handle(line):
    global cfg, cur_acc, _upd_buf, _upd_mode

    try:    cmd = json.loads(line)
    except: return

    c = cmd.get('cmd','')

    if c == 'ping':
        gt_send({'ok':True,'msg':'pong','version':VERSION,'device':'GeekToken'})

    elif c == 'status':
        uptime = time.ticks_diff(time.ticks_ms(), start_ms) // 1000
        gt_send({'ok':True,'wifi':wifi_ok,'time_synced':time_synced,
                 'uptime':uptime,'accounts':len(cfg['accounts']),'version':VERSION})

    elif c == 'get_accounts':
        gt_send({'ok':True,'accounts':[a['label'] for a in cfg['accounts']]})

    elif c == 'add_account':
        label  = cmd.get('label','').strip()
        secret = cmd.get('secret','').strip()
        if not label or not secret:
            gt_send({'ok':False,'err':'missing label or secret'}); return
        if any(a['label']==label for a in cfg['accounts']):
            gt_send({'ok':False,'err':'already exists'}); return
        cfg['accounts'].append({'label':label,'secret':secret})
        save_cfg(cfg)
        gt_send({'ok':True})

    elif c == 'del_account':
        label  = cmd.get('label','')
        before = len(cfg['accounts'])
        cfg['accounts'] = [a for a in cfg['accounts'] if a['label']!=label]
        save_cfg(cfg)
        gt_send({'ok':True,'removed':before-len(cfg['accounts'])})

    elif c == 'set_wifi':
        cfg['wifi']['ssid'] = cmd.get('ssid','')
        cfg['wifi']['pass'] = cmd.get('pass','')
        save_cfg(cfg)
        gt_send({'ok':True})

    elif c == 'sync_time':
        asyncio.create_task(do_wifi_ntp())
        gt_send({'ok':True})

    elif c == 'check_ota':
        asyncio.create_task(do_wifi_ntp(check_update=True))
        gt_send({'ok':True,'msg':'OTA check started'})

    elif c == 'next_account':
        if cfg['accounts']: cur_acc = (cur_acc+1) % len(cfg['accounts'])
        gt_send({'ok':True})

    elif c == 'get_code':
        label = cmd.get('label','')
        acc = next((a for a in cfg['accounts'] if a['label']==label), None)
        if not acc and cfg['accounts']:
            acc = cfg['accounts'][cur_acc % len(cfg['accounts'])]
        if not acc:
            gt_send({'ok':False,'err':'no accounts'}); return
        try:
            gt_send({'ok':True,'code':'{:06d}'.format(totp(acc['secret'])),
                     'remaining':remaining(),'label':acc['label']})
        except Exception as e:
            gt_send({'ok':False,'err':str(e)})

    elif c == 'set_led':
        set_led(cmd.get('mode','heartbeat'))
        gt_send({'ok':True})

    elif c == 'update_start':
        _upd_buf = []; _upd_mode = True
        gt_send({'ok':True,'ready':True})

    elif c == 'ul':
        if _upd_mode: _upd_buf.append(cmd.get('l',''))

    elif c == 'update_end':
        if not _upd_mode:
            gt_send({'ok':False,'err':'not in update mode'}); return
        _upd_mode = False
        try:
            with open('main_new.py','w') as f: f.write('\n'.join(_upd_buf))
            import uos
            try: uos.remove('main.py')
            except: pass
            uos.rename('main_new.py','main.py')
            gt_send({'ok':True,'lines':len(_upd_buf)})
            await asyncio.sleep_ms(300); reset()
        except Exception as e:
            gt_send({'ok':False,'err':str(e)})

    elif c == 'reboot':
        gt_send({'ok':True})
        await asyncio.sleep_ms(200); reset()

    else:
        gt_send({'ok':False,'err':'unknown: '+c})

# ═══════════════════════════════════════════════════════════════
#  Async tasks
# ═══════════════════════════════════════════════════════════════

async def protocol_task():
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            line = sys.stdin.readline().strip()
            if line.startswith('{'): await gt_handle(line)
        await asyncio.sleep_ms(20)

async def display_task():
    global oled_dev, cur_acc

    async def init_oled():
        global oled_dev
        while True:
            try:
                i2c      = I2C(0, sda=Pin(4), scl=Pin(5), freq=400_000)
                oled_dev = SSD1306_I2C(i2c)
                return
            except OSError:
                set_led("fast")
                await asyncio.sleep_ms(1000)

    await init_oled()
    show_msg(oled_dev, "GeekToken v2.2", "booting...")
    await asyncio.sleep(1)
    await do_wifi_ntp(check_update=True)

    cycle = 0
    while True:
        try:
            accs = cfg['accounts']
            if accs:
                acc = accs[cur_acc % len(accs)]
                try:
                    show_code(oled_dev, totp(acc['secret']), remaining(), acc['label'])
                    if len(accs) > 1:
                        cycle += 1
                        if cycle >= 20: cycle = 0; cur_acc = (cur_acc+1) % len(accs)
                except (ValueError, IndexError):
                    show_msg(oled_dev, "Bad secret!", acc['label'][:16])
            else:
                show_msg(oled_dev, "No accounts", "Use Manager app")
        except OSError:
            set_led("fast")
            await asyncio.sleep_ms(2000)
            await init_oled()
            set_led("heartbeat")
        await asyncio.sleep_ms(500)

async def main():
    await asyncio.gather(display_task(), protocol_task(), led_task())

asyncio.run(main())
