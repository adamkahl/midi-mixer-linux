#!/usr/bin/env python3
"""MIDI Mixer for Linux: control per-app volume/mute with a MIDI controller.
Deps: python3-gi (GTK3), pactl (PulseAudio/PipeWire), aseqdump + aconnect (alsa-utils), libasound2."""
import ctypes, json, os, re, shutil, signal, subprocess, sys, threading, time
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gtk, Gdk, GdkX11, Gio, GLib

CFG = os.path.expanduser("~/.config/midi-mixer-linux/config.json")
AUTOSTART = os.path.expanduser("~/.config/autostart/midi-mixer.desktop")
HERE = os.path.dirname(os.path.realpath(__file__))  # real folder, even when started through a symlink
ICON_FILE = os.path.join(HERE, "midi-mixer.svg")  # the mixer's own icon (falls back to the theme's volume icon)
ICON = "midi-mixer" if os.path.exists(ICON_FILE) else "multimedia-volume-control"
SPECIAL = {"@master": "Master output", "@mic": "Microphone", "@other": "Unassigned apps"}

# Controllers with a fixed layout. While one is connected the mixer shows exactly these channels.
# Each control: (label, fader/knob key, mute-button key, default name, default targets). MIDI channel 11 = "10".
PROFILES = {
    "X-TOUCH MINI": {  # Behringer X-Touch Mini, standard mode, layer A, factory map
        "title": "X-Touch Mini",
        "led_on": 1,  # button LEDs: velocity 1 = on (2 = blink)
        # Button LEDs are driven on the global channel (1): top row notes 0-7, bottom row notes 8-15
        "leds": {f"note:10:{n}": f"note:0:{n - 8}" for n in range(8, 24)},
        # Knob rings: CC 9-16 on the global channel set the LEDs directly; 0 = all off (for disabled channels)
        "rings": {f"cc:10:{i}": f"cc:0:{i + 8}" for i in range(1, 9)},
        # Sent on connect (raw values, global channel): CC 127 = 0 -> standard mode (not MC mode);
        # CC 1-8 = 2 -> rings in "Fan" mode, filling left to right like a volume bar
        "init": [("cc", 0, 127, 0)] + [("cc", 0, i, 2) for i in range(1, 9)],
        "controls": [(f"Knob {i}", f"cc:10:{i}", None, f"Knob {i}", []) for i in range(1, 9)]
        + [("Fader", "cc:10:9", None, "Master", ["@master"])],
        # Every button can be given an action in the window (gamepad button -> Controller buttons).
        "buttons": {**{f"Top {i}": f"note:10:{7 + i}" for i in range(1, 9)},
                    **{f"Bottom {i}": f"note:10:{15 + i}" for i in range(1, 9)}},
        # Defaults: top row mutes the knob above it, bottom 7 mutes the fader (Master), bottom 8 shows the mixer
        "defaults": {**{f"note:10:{7 + i}": {"do": "mute", "channel": i - 1} for i in range(1, 9)},
                     "note:10:22": {"do": "mute", "channel": 8}, "note:10:23": {"do": "window"}},
    },
}

CSS = b"""
window, .mixer { background: #17181c; }
headerbar { background: #202127; border: none; box-shadow: none; color: #e8e8ea; }
.strip { background: #24262d; border-radius: 10px; padding: 6px 5px; border: 1px solid #2e3038; }
.strip button { background: #2f323a; background-image: none; color: #c9cad0; border: none;
  box-shadow: none; text-shadow: none; border-radius: 6px; padding: 2px 4px; min-height: 0; }
.strip button:hover { background: #3a3d47; }
.strip entry { background: transparent; border: none; box-shadow: none; color: #f2f2f4;
  font-weight: bold; font-size: 9.5pt; min-height: 0; padding: 2px 0; }
.strip entry:focus { background: #1b1c21; }
.strip .assign { font-size: 8pt; color: #8b8f9c; }
.strip .close { padding: 0; min-width: 20px; min-height: 20px; background: transparent; color: #6b6f7a; }
.strip .close:hover { color: #e5484d; }
.strip .control { font-size: 7pt; color: #6b6f7a; letter-spacing: 1px; }
scale trough { min-width: 8px; border-radius: 4px; background: #33363f; border: none; }
scale highlight { background: #4f8cff; border-radius: 4px; border: none; }
.muted scale highlight { background: #555863; }
scale slider { min-width: 20px; min-height: 20px; border-radius: 10px; background: #f2f2f4;
  border: none; box-shadow: 0 1px 4px rgba(0,0,0,.5); }
scale value { color: #f2f2f4; font-weight: bold; font-size: 10pt; }
.strip .mute { font-weight: bold; font-size: 8.5pt; letter-spacing: 1px; padding: 4px; }
.strip .mute:checked { background: #e5484d; color: #fff; }
.strip .learn { font-size: 7.5pt; color: #8b8f9c; border-radius: 8px; padding: 1px 4px; }
.strip .learn.active { background: #f5a524; color: #17181c; }
.strip.disabled { background: #1d1e23; border-color: #24262d; }
.strip.disabled scale, .strip.disabled .mute, .strip.disabled entry { opacity: 0.35; }
.osd-window { background: transparent; }
.osd { background: rgba(32, 33, 39, 0.96); border-radius: 16px; padding: 14px 18px; border: 1px solid #2e3038; }
.osd label { color: #f2f2f4; }
.osd .osd-title { font-weight: bold; font-size: 12pt; }
.osd .osd-detail { color: #8b8f9c; font-size: 9pt; }
.osd progressbar trough { min-height: 8px; border-radius: 4px; background: #33363f; border: none; }
.osd progressbar progress { min-height: 8px; border-radius: 4px; background: #4f8cff; border: none; }
.osd-window.muted .osd progressbar progress { background: #555863; }
"""
EVT = re.compile(r"^\s*(\d+):\d+\s+(Control change|Note on|Note off|Pitch bend|Program change)\s+(\d+), "
                 r"(?:controller|note|value|program) (-?\d+)(?:, (?:value|velocity) (\d+))?")


def nice(key):
    """'cc:0:7' -> 'CC 7', 'note:1:60' -> 'Note 60 · ch2', 'pb:0' -> 'Pitch bend'."""
    if not key:
        return "—"
    kind, ch, *n = key.split(":")
    return {"cc": "CC", "note": "Note", "pb": "Pitch bend"}[kind] + (f" {n[0]}" if n else "") + \
        (f" · ch{int(ch) + 1}" if ch != "0" else "")


def profile_of(name):
    return next((p for p in PROFILES if p in name.upper()), None)


def owns(app, name, targets, assigned):
    """Does a channel with `targets` control this stream? Apps match by program or display name."""
    ids = {app, name.lower()}
    return bool(ids & set(targets)) or ("@other" in targets and not ids & assigned)


def die_with_parent():
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def run(*a, check=False):
    r = subprocess.run(a, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(a[:2])} failed (audio system not ready?)")
    return r.stdout


def pct(s):
    return int(m.group(1)) if (m := re.search(r"(\d+)%", s)) else 0


class Audio:
    """pactl wrapper. Commands are coalesced per key and run on one worker thread."""

    def __init__(self, on_streams):
        self.on_streams, self.streams, self.devs, self.jobs = on_streams, {}, {}, {}
        self.cv = threading.Condition()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._subscribe, daemon=True).start()

    def submit(self, key, fn):
        with self.cv:
            self.jobs[key] = fn
            self.cv.notify()

    def _worker(self):
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                fn = self.jobs.pop(next(iter(self.jobs)))
            try:
                fn()
            except Exception as e:
                print("audio:", e)

    def _subscribe(self):
        """Follow audio events. At login the audio system may not be up yet, and it can restart later:
        whenever the listener stops, wait for the audio system, catch up, and listen again."""
        while True:
            while subprocess.run(["pactl", "info"], capture_output=True).returncode:
                time.sleep(1)
            p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True,
                                 stderr=subprocess.DEVNULL, preexec_fn=die_with_parent)
            self.submit("refresh", self._refresh)  # streams that appeared while we weren't listening
            for line in p.stdout:
                if re.search(r"on (sink|source|server)", line):
                    self.submit("refresh", self._refresh)
            p.wait()
            time.sleep(1)

    def _refresh(self):
        streams = {}  # index -> (app, name, vol, muted)
        for s in json.loads(run("pactl", "-f", "json", "list", "sink-inputs", check=True) or "[]"):
            p = s.get("properties", {})
            app = (p.get("application.process.binary") or p.get("application.name") or "?").lower()
            vol = max((pct(c["value_percent"]) for c in s.get("volume", {}).values()), default=0)
            streams[s["index"]] = (app, p.get("application.name") or app, vol, s.get("mute", False))
        devs = {}  # default output/input, if there is one (a failed reading is skipped, never taken as 0%)
        for t, kind in (("@master", "sink"), ("@mic", "source")):
            if vol := run("pactl", f"get-{kind}-volume", f"@DEFAULT_{kind.upper()}@"):
                devs[t] = (pct(vol), "yes" in run("pactl", f"get-{kind}-mute", f"@DEFAULT_{kind.upper()}@"))
        new = {i: v for i, v in streams.items() if i not in self.streams}
        self.streams, self.devs = streams, devs
        GLib.idle_add(self.on_streams, new)

    def apply(self, key, targets, vol, mute, assigned, only=None):
        """Set volume (0-100) and mute on targets. `only` limits app targets to given stream ids."""
        def job():
            m = "1" if mute else "0"
            for t in targets:
                if t == "@master" and only is None:
                    run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{vol}%")
                    run("pactl", "set-sink-mute", "@DEFAULT_SINK@", m)
                elif t == "@mic" and only is None:
                    run("pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{vol}%")
                    run("pactl", "set-source-mute", "@DEFAULT_SOURCE@", m)
            for i, (app, name, *_) in list(self.streams.items()):
                if (only is None or i in only) and owns(app, name, targets, assigned):
                    run("pactl", "set-sink-input-volume", str(i), f"{vol}%")
                    run("pactl", "set-sink-input-mute", str(i), m)
        self.submit(key, job)


class SeqEvent(ctypes.Structure):  # snd_seq_event_t with the data union laid out as snd_seq_ev_ctrl_t
    _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("tag", ctypes.c_ubyte),
                ("queue", ctypes.c_ubyte), ("time", ctypes.c_uint * 2), ("src", ctypes.c_ubyte * 2),
                ("dst", ctypes.c_ubyte * 2), ("channel", ctypes.c_ubyte), ("b", ctypes.c_ubyte * 3),
                ("param", ctypes.c_uint), ("value", ctypes.c_int)]


class Midi:
    """Input: all ALSA sequencer devices via aseqdump. Output (LEDs/rings/motor faders): libasound.
    Every 2 s, any device not connected to us (new, or replugged) gets connected."""

    def __init__(self, on_msg, on_devices):
        self.on_msg, self.on_devices, self.outs, self.names = on_msg, on_devices, {}, {}
        self.asound, self.seq, self.p, self.ok = ctypes.CDLL("libasound.so.2"), None, None, None
        self._connect()
        GLib.timeout_add_seconds(2, self._connect)

    def _start(self):
        """Open the ALSA sequencer. Fails without access to /dev/snd/seq (e.g. in a remote-desktop session)."""
        if self.seq and self.p and self.p.poll() is None:
            return True
        if self.p:
            self.p.kill()
        seq = ctypes.c_void_p()
        if not os.access("/dev/snd/seq", os.R_OK | os.W_OK) \
                or self.asound.snd_seq_open(ctypes.byref(seq), b"default", 1, 0) < 0:  # SND_SEQ_OPEN_OUTPUT
            self.seq = self.p = None
            return False
        self.p = subprocess.Popen(["stdbuf", "-oL", "aseqdump"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, preexec_fn=die_with_parent)
        if not (m := re.search(r"port (\d+):", self.p.stdout.readline())):
            self.p.kill()
            self.asound.snd_seq_close(seq)
            self.seq = self.p = None
            return False
        self.me, self.seq = m.group(1), seq
        self.asound.snd_seq_set_client_name(seq, b"MIDI Mixer")
        self.port = self.asound.snd_seq_create_simple_port(seq, b"feedback", 0x21, 0x100002)
        self.out = str(self.asound.snd_seq_client_id(seq))
        threading.Thread(target=self._read, args=(self.p,), daemon=True).start()
        return True

    def _ports(self, flag, linked_by, addr):
        """({'client:port': device name}, {ports already linked with `addr`}) from `aconnect -i/-o -l`."""
        ports, linked, client, port, name = {}, set(), None, None, ""
        for line in run("aconnect", flag, "-l").splitlines():
            if m := re.match(r"client (\d+): '(.*?)'", line):
                ok = m.group(1) not in ("0", "14") and not re.match(r"PipeWire|MIDI Mixer|aseqdump", m.group(2))
                client, name, port = (m.group(1) if ok else None), m.group(2), None
                self.names[m.group(1)] = name
            elif client and (m := re.match(r"\s+(\d+) '", line)):
                port = f"{client}:{m.group(1)}"
                ports[port] = name
            elif port and linked_by in line and re.search(rf"(?<![\d:]){addr}(?!\d)", line):
                linked.add(port)
        return ports, linked

    def _connect(self):
        if (ok := self._start()) != self.ok:  # MIDI became (un)available: let the window say so
            self.ok, self.outs = ok, {}
            GLib.idle_add(self.on_devices)
        if not ok:
            return True
        me, feedback = f"{self.me}:0", f"{self.out}:{self.port}"
        (ins, linked_in), (outs, linked_out) = self._ports("-i", "Connecting To:", me), \
            self._ports("-o", "Connected From:", feedback)
        for p in ins.keys() - linked_in:
            run("aconnect", p, me)
        for p in outs.keys() - linked_out:  # hardware outputs only open while subscribed to
            run("aconnect", feedback, p)
        if set(outs) != set(self.outs) or ins.keys() - linked_in or outs.keys() - linked_out:
            self.outs = outs
            GLib.idle_add(self.on_devices)
        return True

    def devices(self):
        return set(self.outs.values())

    def send(self, key, val, only=None):
        """Send `val` back to control `key`: 0..1 (a level or on/off), an int (raw MIDI value),
        or None (lights off, e.g. a disabled channel's knob ring)."""
        if not key or not self.seq:
            return
        for port, name in self.outs.items():
            if only and only not in name.upper():
                continue
            prof = PROFILES.get(profile_of(name), {})
            k, v = (prof.get("rings", {}).get(key, key), 0) if val is None else (key, val)
            kind, ch, *num = prof.get("leds", {}).get(k, k).split(":")
            ev = SeqEvent(queue=253, channel=int(ch))  # 253 = deliver immediately
            ev.src[1] = self.port
            ev.dst[0], ev.dst[1] = map(int, port.split(":"))
            raw = type(v) is int
            if kind == "cc":
                ev.type, ev.param, ev.value = 10, int(num[0]), v if raw else round(v * 127)
            elif kind == "note":
                ev.type, ev.b[0], ev.b[1] = 6, int(num[0]), prof.get("led_on", 127) if v else 0
            else:
                ev.type, ev.value = 13, v if raw else round(v * 16383) - 8192
            self.asound.snd_seq_event_output_direct(self.seq, ctypes.byref(ev))

    def _read(self, p):
        for line in p.stdout:
            if m := EVT.match(line):
                src, kind, ch, num, val = m.group(1), m.group(2), m.group(3), int(m.group(4)), m.group(5)
                if kind == "Program change":  # e.g. the X-Touch's layer A/B buttons
                    GLib.idle_add(self.on_msg, f"pc:{ch}", num, False)
                elif kind == "Pitch bend":
                    GLib.idle_add(self.on_msg, f"pb:{ch}", (num + 8192) / 16383, True)
                elif kind == "Control change":
                    GLib.idle_add(self.on_msg, f"cc:{ch}:{num}", int(val) / 127, True)
                else:  # note off / velocity 0 = button released
                    on = kind == "Note on" and int(val or 0) > 0
                    GLib.idle_add(self.on_msg, f"note:{ch}:{num}", 1.0 if on else 0.0, False)


class Strip(Gtk.Box):
    """One channel. `control` = (label, fader key, mute key) when it belongs to a controller profile:
    then it's fixed to that knob/button and can't be removed or re-bound."""

    def __init__(self, app, d, control=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.get_style_context().add_class("strip")
        self.set_size_request(88, -1)
        self.app, self.targets, self.control = app, list(d.get("targets", [])), control
        self.fader_key, self.mute_key = (control[1], control[2]) if control else (d.get("fader"), d.get("mute"))
        self.touched, self.syncing, self.active = 0, False, None
        self.name = Gtk.Entry(text=d.get("name", "Channel"), width_chars=6, xalign=0.5)
        self.name.connect("changed", lambda *_: app.save())
        self.pick_label = Gtk.Label(ellipsize=3, max_width_chars=9)  # PANGO_ELLIPSIZE_END
        self.pick = Gtk.MenuButton(popover=Gtk.Popover(), relief=Gtk.ReliefStyle.NONE)
        self.pick.add(self.pick_label)
        self.pick.get_style_context().add_class("assign")
        self.pick.connect("toggled", self._fill)
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, inverted=True, vexpand=True,
                               adjustment=Gtk.Adjustment(value=d.get("vol", 100), upper=100, step_increment=1))
        self.scale.set_digits(0)
        self.scale.set_value_pos(Gtk.PositionType.TOP)
        self.scale.connect("value-changed", lambda *_: self.changed())
        self.mute = Gtk.ToggleButton(label="MUTE", active=d.get("muted", False))
        self.mute.get_style_context().add_class("mute")
        self.mute.connect("toggled", lambda *_: self.changed())
        top = Gtk.Box()
        top.pack_start(self.name, True, True, 0)
        widgets = [top, self.pick, self.scale, self.mute]
        if control:
            caption = Gtk.Label(label=control[0].upper())
            caption.get_style_context().add_class("control")
            widgets.append(caption)
        else:
            self.lf, self.lm = Gtk.Button(), Gtk.Button()
            for b, what in ((self.lf, "fader"), (self.lm, "mute")):
                b.get_style_context().add_class("learn")
                b.set_tooltip_text(f"Click, then touch the {'knob/fader' if what == 'fader' else 'mute button'} "
                                   "on your controller to bind it")
                b.connect("clicked", lambda _, w=what: app.learn(self, w))
            rm = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
            rm.get_style_context().add_class("close")
            rm.set_tooltip_text("Remove channel")
            rm.connect("clicked", lambda *_: app.remove(self))
            top.pack_end(rm, False, False, 0)
            widgets += [self.lf, self.lm]
        for w in widgets:
            self.pack_start(w, w is self.scale, True, 0)
        self.labels()
        self.style_mute()
        self.set_active(False)  # until the first audio refresh says what's in use

    def labels(self, learning=None):
        if not self.control:
            for b, what, key in ((self.lf, "fader", self.fader_key), (self.lm, "mute", self.mute_key)):
                ctx = b.get_style_context()
                (ctx.add_class if learning == what else ctx.remove_class)("active")
                b.set_label(("Move a knob…" if what == "fader" else "Press a button…") if learning == what
                            else ("Knob: " if what == "fader" else "Button: ") + nice(key))
        streams = self.app.audio.streams if hasattr(self.app, "audio") else {}
        apps = {**self.app.cfg.get("apps", {}), **{a: n for a, n, *_ in streams.values()}}
        names = [SPECIAL.get(t) or apps.get(t) or next((n for n in apps.values() if n.lower() == t), t)
                 for t in self.targets]
        self.pick_label.set_text((names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "") if names
                                  else "Assign") + " ▾")
        self.pick.set_tooltip_text(", ".join(names) or "Choose what this channel controls")
        self.names = names

    def style_mute(self):
        ctx = self.get_style_context()
        (ctx.add_class if self.mute.get_active() else ctx.remove_class)("muted")

    def set_active(self, on):
        """A channel with nothing assigned is greyed out, ignores its knob, and is dark on the controller."""
        if on == self.active:
            return
        if on and self.active is False and self.fader_key:  # ring was switched off: nudge it so it redraws
            raw = round(self.scale.get_value() * 1.27)
            self.app.midi.send(self.fader_key, raw - 1 if raw else 1)
        self.active = on
        (self.get_style_context().remove_class if on else self.get_style_context().add_class)("disabled")
        self.scale.set_sensitive(on)
        self.mute.set_sensitive(on)
        self.set_tooltip_text(None if on else "Not in use: choose an app with Assign")
        self.feedback()

    def _fill(self, btn):
        if not btn.get_active():
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=2)
        running = {a: n for a, n, *_ in self.app.audio.streams.values()}
        names = {**self.app.cfg.get("apps", {}), **running}  # every app that has ever played audio
        for t in self.targets:  # typed names: show once, under the matching app's real name if it has played
            if t not in names and t not in SPECIAL:
                k = next((k for k in names if names[k].lower() == t), None)
                names[t] = names.pop(k) if k else t
                if k in running:
                    running[t] = running[k]
        for t in [*SPECIAL, *sorted(names, key=lambda t: names[t].lower())]:
            cb = Gtk.CheckButton(label=SPECIAL.get(t) or names[t] + ("" if t in running else " (not running)"),
                                 active=t in self.targets)
            cb.connect("toggled", self._toggle, t)
            box.add(cb)
        other = Gtk.Entry(placeholder_text="Other app (e.g. brave)…", margin_top=4)
        other.set_tooltip_text("Type a program or app name and press Enter, for apps that haven't played audio yet")
        other.connect("activate", self._add_other)
        box.add(other)
        box.show_all()
        old = btn.get_popover().get_child()
        if old:
            old.destroy()
        btn.get_popover().add(box)

    def _add_other(self, entry):
        t = entry.get_text().strip().lower()
        known = {**self.app.cfg.get("apps", {}), **{a: n for a, n, *_ in self.app.audio.streams.values()}}
        t = next((k for k, n in known.items() if t in (k, n.lower())), t)  # "Brave" -> its program id
        if t and t not in self.targets:
            self._toggle(None, t)
        GLib.idle_add(self._fill, self.pick)  # refresh the list (stays open, so more apps can be added)

    def _toggle(self, cb, t):
        """Tick/untick an app. The first app on an empty channel keeps its own volume (the fader picks it up);
        apps added after that join at the channel's current level, so the fader never jumps."""
        adding, had_apps = cb is None or cb.get_active(), bool(self.targets)
        (self.targets.append if adding else self.targets.remove)(t)
        self.app.save()
        if adding and had_apps:
            self.touched = time.monotonic()  # keep the fader where it is...
            self.apply()                     # ...and bring the new app to that level and mute state
        elif adding:
            self.touched = 0  # first app: let the fader take on the app's current level
        else:
            self.touched = time.monotonic()  # removing an app leaves the fader alone
        self.app.on_streams({})

    def changed(self):
        """User moved the slider/fader or toggled mute: push to audio and controller."""
        self.style_mute()
        if self.syncing:
            return
        self.touched = time.monotonic()
        self.apply()
        self.feedback()

    def sync(self, vol, muted):
        """Reflect the real audio level (changed by another app, or on startup) in the GUI and controller."""
        if time.monotonic() - self.touched < 1 or (vol, muted) == (self.scale.get_value(), self.mute.get_active()):
            return  # skip echoes of our own recent changes so faders don't jump back
        self.syncing = True
        self.scale.set_value(vol)
        self.mute.set_active(muted)
        self.syncing = False
        self.feedback()

    def feedback(self):
        """Show this channel on the controller: knob ring = level, button light = muted. Disabled: all dark."""
        on = bool(self.active)
        self.app.midi.send(self.fader_key, self.scale.get_value() / 100 if on else None)
        for key in self.app.mute_buttons(self):
            self.app.midi.send(key, on and self.mute.get_active())

    def apply(self, only=None):
        self.app.audio.apply(id(self) if only is None else (id(self), tuple(only)), self.targets,
                             int(self.scale.get_value()), self.mute.get_active(), self.app.assigned(), only)
        if only is None:
            self.app.save()

    def dump(self):
        d = {"name": self.name.get_text(), "targets": self.targets, "vol": int(self.scale.get_value()),
             "muted": self.mute.get_active()}
        return d if self.control else {**d, "fader": self.fader_key, "mute": self.mute_key}


class TrayItem:
    """System tray icon over the StatusNotifierItem D-Bus protocol (Xfce, KDE, ...), whose clicks arrive
    reliably, unlike old-style embedded tray icons. Left click -> on_activate; right click -> a D-Bus menu
    of `items`: (label, callback, is_checked or None), or None for a separator."""
    XML = """<node><interface name="org.kde.StatusNotifierItem">
      <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
      <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
      <property name="IconName" type="s" access="read"/><property name="IconThemePath" type="s" access="read"/>
      <property name="ItemIsMenu" type="b" access="read"/>
      <property name="Menu" type="o" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
      <method name="Activate"><arg type="i"/><arg type="i"/></method>
      <method name="SecondaryActivate"><arg type="i"/><arg type="i"/></method>
      <method name="ContextMenu"><arg type="i"/><arg type="i"/></method>
      <method name="Scroll"><arg type="i"/><arg type="s"/></method></interface>
    <interface name="com.canonical.dbusmenu">
      <property name="Version" type="u" access="read"/><property name="Status" type="s" access="read"/>
      <property name="TextDirection" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
      <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
        <arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
      <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
        <arg type="a(ia{sv})" direction="out"/></method>
      <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
        <arg type="v" direction="in"/><arg type="u" direction="in"/></method>
      <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
      <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
      <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
        <arg type="ai" direction="out"/></method>
      <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal></interface></node>"""

    def __init__(self, on_activate, items):
        self.on_activate, self.items, self.revision = on_activate, items, 1
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.props = {"Category": GLib.Variant("s", "Hardware"), "Id": GLib.Variant("s", "midi-mixer"),
                      "Title": GLib.Variant("s", "MIDI Mixer"), "Status": GLib.Variant("s", "Active"),
                      "IconName": GLib.Variant("s", ICON), "IconThemePath": GLib.Variant("s", HERE),
                      "ItemIsMenu": GLib.Variant("b", False),
                      "Menu": GLib.Variant("o", "/MenuBar"),
                      "ToolTip": GLib.Variant("(sa(iiay)ss)", (ICON, [], "MIDI Mixer", "")),
                      "Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr")}
        menu_only = {"Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}
        node = Gio.DBusNodeInfo.new_for_xml(self.XML)
        get = lambda c, sender, path, iface, prop, *_: \
            menu_only[prop] if iface == "com.canonical.dbusmenu" and prop in menu_only else self.props[prop]
        self.bus.register_object("/StatusNotifierItem", node.interfaces[0], self._call, get, None)
        self.bus.register_object("/MenuBar", node.interfaces[1], self._call, get, None)
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
                                         lambda *_: self._register(), None)  # (re-)register when the panel starts

    @staticmethod
    def available():
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                             GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)), None, 0, -1, None)[0]

    def _register(self):
        self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                      "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None, 0, -1, None)

    def _entry(self, i):
        if self.items[i - 1] is None:
            return {"type": GLib.Variant("s", "separator")}
        label, _, checked = self.items[i - 1]
        props = {"label": GLib.Variant("s", label)}
        if checked:
            props.update({"toggle-type": GLib.Variant("s", "checkmark"), "toggle-state": GLib.Variant("i", int(checked()))})
        return props

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (i, self._entry(i), [])) for i in range(1, len(self.items) + 1)]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.on_activate)
        elif method == "GetLayout":
            return invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or range(1, len(self.items) + 1)
            return invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._entry(i)) for i in ids if i > 0],)))
        elif method in ("Event", "EventGroup"):
            events = [params.unpack()] if method == "Event" else params.unpack()[0]
            for i, event, *_ in events:
                if event == "clicked" and 0 < i <= len(self.items) and self.items[i - 1]:
                    GLib.idle_add(self._click, i)
            if method == "EventGroup":
                return invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            return invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            return invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        invocation.return_value(None)

    def _click(self, i):
        self.items[i - 1][1]()
        self.revision += 1  # e.g. "Start on login" changed its checkmark
        self.bus.emit_signal(None, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated",
                             GLib.Variant("(ui)", (self.revision, 0)))


class Osd(Gtk.Window):
    """On-screen overlay showing a channel's level while it's adjusted from the controller."""

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        screen = self.get_screen()
        if screen.is_composited() and screen.get_rgba_visual():
            self.set_visual(screen.get_rgba_visual())  # rounded corners
        self.set_app_paintable(True)
        self.get_style_context().add_class("osd-window")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class("osd")
        self.title, self.detail = Gtk.Label(xalign=0), Gtk.Label(xalign=0, ellipsize=3, max_width_chars=40)
        self.title.get_style_context().add_class("osd-title")
        self.detail.get_style_context().add_class("osd-detail")
        self.row = row = Gtk.Box(spacing=12)
        self.bar, self.value = Gtk.ProgressBar(valign=Gtk.Align.CENTER), Gtk.Label(width_chars=6, xalign=1)
        row.pack_start(self.bar, True, True, 0)
        row.pack_start(self.value, False, False, 0)
        for w in (self.title, self.detail, row):
            box.add(w)
        self.add(box)
        box.show_all()
        self.set_size_request(340, -1)
        self.timer = None

    def show_message(self, title, detail=""):
        self.show_level(title, detail, 0, False, bar=False)

    def show_level(self, title, detail, vol, muted, bar=True):
        self.row.set_visible(bar)
        self.title.set_text(title)
        self.detail.set_text(detail)
        self.detail.set_visible(bool(detail) and detail != title)
        self.bar.set_fraction(vol / 100)
        self.value.set_text("Muted" if muted else f"{int(vol)}%")
        (self.get_style_context().add_class if muted else self.get_style_context().remove_class)("muted")
        display = Gdk.Display.get_default()
        geo = (display.get_primary_monitor() or display.get_monitor(0)).get_geometry()
        w, h = max(340, self.get_preferred_width()[1]), self.get_preferred_height()[1]
        self.move(geo.x + (geo.width - w) // 2, geo.y + geo.height - h - 120)
        self.show()
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(1500, self._hide)

    def _hide(self):
        self.timer = None
        self.hide()
        return False


class App(Gtk.ApplicationWindow):
    def __init__(self, application, hidden):
        Gtk.IconTheme.get_default().append_search_path(HERE)  # so ICON ("midi-mixer") is found by name
        super().__init__(application=application, title="MIDI Mixer", icon_name=ICON)
        if os.path.exists(ICON_FILE):
            Gtk.Window.set_default_icon_from_file(ICON_FILE)  # window/taskbar icon (also the overlay and dialogs)
            self.set_icon_from_file(ICON_FILE)
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", True)
        settings.set_property("gtk-entry-select-on-focus", False)  # don't open with a channel name selected
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.learning, self._save_id, self.strips, self.profile = None, None, [], None
        self.bar = Gtk.HeaderBar(title="MIDI Mixer", show_close_button=True)
        self.add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        self.add_btn.set_tooltip_text("Add channel")
        self.add_btn.connect("clicked", lambda *_: self.add_strip({"name": f"Channel {len(self.strips) + 1}"}))
        self.bar.pack_end(self.add_btn)
        self.keys_btn = Gtk.MenuButton(popover=Gtk.Popover(),
                                       image=Gtk.Image.new_from_icon_name("input-gaming-symbolic", Gtk.IconSize.BUTTON))
        self.keys_btn.set_tooltip_text("Controller buttons")
        self.keys_btn.connect("toggled", self.fill_keys)
        self.bar.pack_end(self.keys_btn)
        self.set_titlebar(self.bar)
        self.box = Gtk.Box(spacing=6, margin=8)
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_width=True)
        scroll.get_style_context().add_class("mixer")
        scroll.add(self.box)
        self.add(scroll)
        self.set_default_size(-1, 340)
        self.connect("delete-event", self.on_close)
        self.connect("show", lambda *_: self.button_lights({"window"}))
        self.connect("hide", lambda *_: (self.set_keep_above(False), self.button_lights({"window"})))
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # logout/shutdown/Ctrl+C: save first
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.quit)
        self.tray()
        if os.path.exists(AUTOSTART):
            self.autostart(True)  # refresh the login entry (e.g. its icon) for this version
        try:
            self.cfg = json.load(open(CFG))
        except ValueError:  # unreadable: keep it aside rather than overwrite the user's assignments
            os.replace(CFG, CFG + ".broken")
            self.cfg = None
        except OSError:
            self.cfg = None
        self.cfg = self.cfg or {"strips": [{"name": "Master", "targets": ["@master"]}, {"name": "Other", "targets": ["@other"]}]}
        self.cfg.setdefault("profiles", {})
        self.ignored = set(self.cfg.get("ignored", []))  # knobs whose auto-added channel was removed
        self.osd, self.playing, self.sel_key, self._loading = Osd(), False, None, False
        self.watch_media()
        self.midi = Midi(self.on_msg, self.on_devices)
        self.build(self.detect())
        self.audio = Audio(self.on_streams)
        self.get_child().show_all()
        self.bar.show_all()
        self.add_btn.set_visible(not self.profile)
        self.keys_btn.set_visible(bool(PROFILES.get(self.profile, {}).get("buttons")))
        if not hidden:
            self.present()

    # ---- window & tray ----
    def tray(self):
        menu = Gtk.Menu()
        show = Gtk.MenuItem(label="Show / Hide")
        show.connect("activate", lambda *_: self.toggle())
        auto = Gtk.CheckMenuItem(label="Start on login", active=os.path.exists(AUTOSTART))
        auto.connect("toggled", lambda i: self.autostart(i.get_active()))
        quit_ = Gtk.MenuItem(label="Quit")
        quit_.connect("activate", lambda *_: self.quit())
        for i in (show, auto, Gtk.SeparatorMenuItem(), quit_):
            menu.append(i)
        menu.show_all()
        self.menu = menu
        try:  # 1. Mint's XApp tray service (Cinnamon/MATE)
            gi.require_version("XApp", "1.0")
            from gi.repository import XApp
            if not XApp.StatusIcon.any_monitors():
                raise ImportError("no XApp tray service")
            self.icon = XApp.StatusIcon()
            self.icon.set_icon_name(ICON_FILE if os.path.exists(ICON_FILE) else ICON)  # (XApp takes a path)
            self.icon.set_tooltip_text("MIDI Mixer")
            self.icon.set_secondary_menu(menu)
            self.icon.connect("activate", lambda *_: self.toggle())
            return
        except (ValueError, ImportError):
            pass
        if TrayItem.available():  # 2. StatusNotifierItem (Xfce, KDE, ...): old-style icons get no clicks there
            self.icon = TrayItem(self.toggle, [
                ("Show / Hide", self.toggle, None),
                ("Start on login", lambda: self.autostart(not os.path.exists(AUTOSTART)),
                 lambda: os.path.exists(AUTOSTART)),
                None,
                ("Quit", self.quit, None)])
            return
        self.icon = Gtk.StatusIcon(icon_name=ICON, tooltip_text="MIDI Mixer")  # 3. old-style tray icon
        self.icon.connect("popup-menu", lambda i, b, t: menu.popup(None, None, Gtk.StatusIcon.position_menu, i, b, t))
        self.icon.connect("activate", lambda *_: self.toggle())

    def on_close(self, *_):
        """Closing the window keeps the mixer running. Show it again from the tray, or by launching it again."""
        self.hide()
        return True

    def overlay(self):
        """Controller button: put the mixer on top of whatever is on screen (even a fullscreen game), or hide it."""
        if self.is_visible() and self.is_active():
            self.hide()
            return
        self.set_keep_above(True)
        self.realize()
        self.show()
        self.deiconify()
        self.present_with_time(GdkX11.x11_get_server_time(self.get_window()))

    def toggle(self):
        """Tray click: bring the window to the front, or hide it if it's already the active window."""
        if self.is_visible() and self.is_active():
            self.hide()
        else:
            self.realize()  # (never shown yet when started hidden at login)
            self.show()
            self.deiconify()
            self.present_with_time(Gtk.get_current_event_time() or GdkX11.x11_get_server_time(self.get_window()))

    def autostart(self, on):
        if on:
            os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
            with open(AUTOSTART, "w") as f:
                f.write(f"[Desktop Entry]\nType=Application\nName=MIDI Mixer\n"
                        f"Icon={ICON_FILE if os.path.exists(ICON_FILE) else ICON}\n"
                        f'Exec="{sys.executable}" "{os.path.abspath(__file__)}" --hidden\n')
        elif os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)

    def quit(self, *_):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self.write()
        self.get_application().quit()
        return True

    # ---- channels ----
    def detect(self):
        """Layout to show: the connected controller's. With no controller (or no MIDI), keep the last one."""
        devices = self.midi.devices() if self.midi.ok else set()
        if not devices:
            return self.cfg.get("last_profile")
        self.cfg["last_profile"] = next((p for p in map(profile_of, devices) if p), None)
        return self.cfg["last_profile"]

    def build(self, profile):
        """Show the channels of `profile` (a connected controller's fixed layout) or the custom channels."""
        self.store()
        for s in self.strips:
            s.destroy()
        self.strips, self.profile, self.learning = [], profile, None
        if profile:
            saved = self.cfg["profiles"].get(profile, [])
            for i, (label, fader, mute, name, targets) in enumerate(PROFILES[profile]["controls"]):
                self.add_strip(saved[i] if i < len(saved) else {"name": name, "targets": targets},
                               (label, fader, mute))
        else:
            for d in self.cfg["strips"]:
                self.add_strip(d)
        self.status()
        self.add_btn.set_visible(not profile)
        self.keys_btn.set_visible(bool(PROFILES.get(profile, {}).get("buttons")))
        if hasattr(self, "audio"):
            self.on_streams({})
        GLib.idle_add(self.fit)

    # ---- controller buttons: every button can do something ----
    ACTIONS = {"none": "Nothing", "mute": "Mute a channel", "window": "Show / hide mixer",
               "PlayPause": "Play / pause media", "Next": "Next track", "Previous": "Previous track",
               "mic": "Mute microphone", "run": "Run a command"}

    def button_actions(self):
        """{button: action} for the connected controller: the user's choices, else the profile's defaults."""
        prof = PROFILES.get(self.profile, {})
        chosen = self.cfg.get("buttons", {}).get(self.profile, {})
        return {k: chosen.get(k, prof.get("defaults", {}).get(k, {"do": "none"})) for k in prof.get("buttons", {}).values()}

    def channel(self, a):
        i = a.get("channel", -1)
        return self.strips[i] if a.get("do") == "mute" and 0 <= i < len(self.strips) else None

    def mute_buttons(self, strip):
        if not strip.control:
            return [strip.mute_key] if strip.mute_key else []
        return [k for k, a in self.button_actions().items() if self.channel(a) is strip]

    def describe(self, a):
        do = a.get("do", "none")
        if do == "mute":
            s = self.channel(a)
            return f"Mute {s.name.get_text()}" if s else "Mute ?"
        if do == "run":
            return "Run " + (os.path.basename(a["cmd"].split()[0]) if a.get("cmd", "").strip() else "…")
        return {"none": "—", "window": "Show mixer", "PlayPause": "Play / pause", "Next": "Next track",
                "Previous": "Prev track", "mic": "Mute mic"}[do]

    def button_light(self, key, a):
        do = a.get("do")
        on = (do == "mute" and bool((s := self.channel(a)) and s.active and s.mute.get_active())
              or do == "window" and self.is_visible()
              or do == "mic" and hasattr(self, "audio") and self.audio.devs.get("@mic", (0, False))[1]
              or do == "PlayPause" and self.playing)
        self.midi.send(key, bool(on))

    def button_lights(self, kinds=None):
        for key, a in self.button_actions().items():
            if kinds is None or a.get("do") in kinds:
                self.button_light(key, a)

    def press(self, key):
        """A controller button was pressed: do what it's set to do."""
        a = self.button_actions().get(key, {})
        do = a.get("do")
        if do == "mute" and (s := self.channel(a)) and s.active:
            s.mute.set_active(not s.mute.get_active())
            self.osd.show_level(s.name.get_text(), ", ".join(s.names), s.scale.get_value(), s.mute.get_active())
        elif do == "window":
            self.overlay()
        elif do in ("PlayPause", "Next", "Previous"):
            self.media(do)
        elif do == "mic":
            muted = not self.audio.devs.get("@mic", (0, False))[1]
            self.audio.submit("mic", lambda: run("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1" if muted else "0"))
            self.osd.show_message("Microphone", "Muted" if muted else "On")
        elif do == "run" and a.get("cmd", "").strip():
            subprocess.Popen(["sh", "-c", a["cmd"]], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.osd.show_message("Running", a["cmd"])

    # media players (Brave, Spotify, VLC, ...) over MPRIS
    def players(self):
        """[(bus name, "Playing"/"Paused"/"Stopped")] for every media player."""
        bus, found = Gio.bus_get_sync(Gio.BusType.SESSION), []
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
                              None, None, 0, 1000, None)[0]
        for n in (n for n in names if n.startswith("org.mpris.MediaPlayer2.")):
            try:
                st = bus.call_sync(n, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")),
                                   None, 0, 500, None)[0]
                found.append((n, st))
            except GLib.Error:
                pass
        return found

    def media(self, method):
        players = self.players()
        pick = next((n for wanted in ("Playing", "Paused") for n, st in players if st == wanted),
                    players[0][0] if players else None)  # whatever is playing, else whatever was paused
        if not pick:
            return self.osd.show_message(self.ACTIONS[method], "No media player is open")
        Gio.bus_get_sync(Gio.BusType.SESSION).call(pick, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player",
                                                   method, None, None, 0, -1, None)
        self.osd.show_message(self.ACTIONS[method], pick.split(".")[3].capitalize())

    def watch_media(self):
        """Keep `playing` current, so a Play / pause button is lit while something plays."""
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        def update(*_):
            playing = any(st == "Playing" for _, st in self.players())
            if playing != self.playing:
                self.playing = playing
                self.button_lights({"PlayPause"})
        bus.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged", "/org/mpris/MediaPlayer2",
                             None, Gio.DBusSignalFlags.NONE, lambda *a: GLib.idle_add(update))
        bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", None,
                             "org.mpris.MediaPlayer2", Gio.DBusSignalFlags.MATCH_ARG0_NAMESPACE,
                             lambda *a: GLib.idle_add(update))
        GLib.idle_add(update)

    # the editor: a picture of the controller's buttons; pick one (click it or press it), choose its action
    def fill_keys(self, btn):
        if not btn.get_active():
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        box.add(Gtk.Label(label="<b>Controller buttons</b>", use_markup=True, xalign=0))
        hint = Gtk.Label(label="Click a button, or press it on your controller, then choose what it does.", xalign=0)
        hint.get_style_context().add_class("dim-label")
        box.add(hint)
        grid, group, self.key_widgets = Gtk.Grid(row_spacing=6, column_spacing=6), None, {}
        for i, (name, key) in enumerate(PROFILES[self.profile]["buttons"].items()):
            b = Gtk.RadioButton.new_from_widget(group)
            group = group or b
            b.set_mode(False)  # looks like a button, not a radio dot
            b.set_size_request(96, 46)
            label = Gtk.Label(justify=Gtk.Justification.CENTER, ellipsize=3, max_width_chars=12)
            b.add(label)
            b.connect("toggled", lambda b, k=key: b.get_active() and not self._loading and self.select_button(k))
            grid.attach(b, i % 8, i // 8, 1, 1)
            self.key_widgets[key] = (b, label, name)
        box.add(grid)
        self.ed_title = Gtk.Label(xalign=0, use_markup=True)
        self.ed_do, self.ed_channel = Gtk.ComboBoxText(), Gtk.ComboBoxText()
        for do, label in self.ACTIONS.items():
            self.ed_do.append(do, label)
        for i, s in enumerate(self.strips):
            self.ed_channel.append(str(i), f"{s.name.get_text()} ({s.control[0] if s.control else i + 1})")
        self.ed_cmd = Gtk.Entry(placeholder_text="e.g. ~/scripts/start-stream.sh  or  firefox", width_chars=40)
        self.ed_cmd.set_tooltip_text("Runs with sh, so you can use ~, arguments, pipes and &&")
        test = Gtk.Button(label="Try it")
        test.connect("clicked", lambda *_: self.press(self.sel_key))
        row = Gtk.Box(spacing=8)
        for w in (self.ed_title, self.ed_do, self.ed_channel, self.ed_cmd, test):
            row.pack_start(w, w is self.ed_cmd, True, 0)
        self.ed_extra = (self.ed_channel, self.ed_cmd, test)
        box.add(row)
        for w in (self.ed_do, self.ed_channel):
            w.connect("changed", self.edit_button)
        self.ed_cmd.connect("changed", self.edit_button)
        box.show_all()
        if old := btn.get_popover().get_child():
            old.destroy()
        btn.get_popover().add(box)
        self.key_labels()
        self.select_button(self.sel_key if self.sel_key in self.key_widgets else next(iter(self.key_widgets)))

    def key_labels(self):
        for key, a in self.button_actions().items():
            b, label, name = self.key_widgets[key]
            label.set_markup(f"<small>{name}</small>\n{GLib.markup_escape_text(self.describe(a))}")
            b.set_tooltip_text(f"{name}: {self.ACTIONS.get(a.get('do'), '')}" + (f" — {a['cmd']}" if a.get("cmd") else ""))

    def select_button(self, key):
        """Show `key`'s action in the editor (also used when it's pressed on the controller)."""
        self.sel_key, self._loading = key, True
        b, _, name = self.key_widgets[key]
        b.set_active(True)
        a = self.button_actions()[key]
        self.ed_title.set_markup(f"<b>{name}</b>")
        self.ed_do.set_active_id(a.get("do", "none"))
        self.ed_channel.set_active_id(str(a.get("channel", 0)))
        self.ed_cmd.set_text(a.get("cmd", ""))
        self._loading = False
        self.show_editor_fields()

    def show_editor_fields(self):
        do = self.ed_do.get_active_id()
        self.ed_channel.set_visible(do == "mute")
        for w in self.ed_extra[1:]:
            w.set_visible(do == "run")

    def edit_button(self, *_):
        if self._loading or not self.sel_key:
            return
        do = self.ed_do.get_active_id() or "none"
        a = {"do": do}
        if do == "mute":
            a["channel"] = int(self.ed_channel.get_active_id() or 0)
        if do == "run":
            a["cmd"] = self.ed_cmd.get_text().strip()
        self.cfg.setdefault("buttons", {}).setdefault(self.profile, {})[self.sel_key] = a
        self.show_editor_fields()
        self.key_labels()
        self.button_light(self.sel_key, a)
        for s in self.strips:  # mute lights may have moved to/from this button
            s.feedback()
        self.save()

    def fit(self):
        """Make the window wide enough for every channel (up to the monitor's width)."""
        display = Gdk.Display.get_default()
        mon = display.get_monitor_at_window(self.get_window()) if self.get_window() else \
            display.get_primary_monitor() or display.get_monitor(0)
        width = min(self.box.get_preferred_width()[1] + 4, mon.get_geometry().width - 80)
        self.resize(width, self.get_size()[1])
        return False

    def status(self):
        title = PROFILES[self.profile]["title"] if self.profile else None
        plugged = any(profile_of(n) == self.profile for n in self.midi.devices())
        self.bar.set_subtitle("No MIDI connection" if not self.midi.ok
                              else title if not title or plugged else f"{title} not connected")

    def store(self):
        dumps = [s.dump() for s in self.strips]
        if self.profile:
            self.cfg["profiles"][self.profile] = dumps
        elif self.strips:
            self.cfg["strips"] = dumps

    def on_devices(self):
        """A controller was plugged in, replugged or removed."""
        if (p := self.detect()) != self.profile:
            self.build(p)
        self.status()
        self.refresh_lights()
        for s in self.strips:
            s.feedback()

    def refresh_lights(self):
        """Re-send ring mode, button lights and dark rings. The controller resets them itself (e.g. when its
        layer button is pressed). Not repeated on a timer: re-sending the ring mode makes the rings blink."""
        for name, prof in PROFILES.items():
            for kind, ch, num, val in prof.get("init", []):
                self.midi.send(f"{kind}:{ch}:{num}", val, only=name)
        for s in self.strips:
            for key in self.mute_buttons(s):
                self.midi.send(key, bool(s.active) and s.mute.get_active())
            if not s.active:
                self.midi.send(s.fader_key, None)
        self.button_lights({"window", "mic", "PlayPause", "none", "run", "Next", "Previous"})
        return True

    def add_strip(self, d, control=None):
        s = Strip(self, d, control)
        self.strips.append(s)
        self.box.pack_start(s, False, False, 0)
        s.show_all()
        self.save()
        return s

    def remove(self, s):
        if s.fader_key:
            self.ignored.add(s.fader_key)  # don't auto-add it again when that knob is touched
        self.strips.remove(s)
        s.destroy()
        self.save()

    def assigned(self):
        return {t for s in self.strips for t in s.targets}

    def on_streams(self, new):
        """New streams get their channel's volume/mute; otherwise channels follow the real levels."""
        assigned, now = self.assigned(), time.monotonic()
        known = self.cfg.setdefault("apps", {})
        if any(known.get(a) != n for a, n, *_ in self.audio.streams.values()):
            known.update({a: n for a, n, *_ in self.audio.streams.values()})  # remember for the Assign list
            self.save()
        for s in self.strips:
            if ids := [i for i, (a, n, *_) in new.items() if owns(a, n, s.targets, assigned)]:
                s.touched = now
                s.apply(ids)
            levels = [self.audio.devs[t] for t in s.targets if t in self.audio.devs]
            levels += [(v, m) for a, n, v, m in self.audio.streams.values() if owns(a, n, s.targets, assigned)]
            if levels:
                s.sync(*levels[0])
            s.set_active(bool(s.targets))  # assigned = enabled, even while its apps are quiet
            s.labels(self.learning[1] if self.learning and self.learning[0] is s else None)
        self.button_lights({"mic"})

    def learn(self, strip, what):
        if self.learning:
            self.learning[0].labels()
        self.learning = None if self.learning == (strip, what) else (strip, what)
        strip.labels(what if self.learning else None)

    def on_msg(self, key, val, continuous):
        kind = key.split(":")[0]
        if kind == "pc":  # layer switched: the controller reset its rings and lights, so send them again
            self.on_devices()
            return
        if key in PROFILES.get(self.profile, {}).get("buttons", {}).values():
            if not val:  # released: momentary buttons switch their own light off, so put it back
                self.button_light(key, self.button_actions()[key])
            elif self.keys_btn.get_active():  # editor open: pressing a button picks it for editing
                self.select_button(key)
            else:
                self.press(key)
            return
        if not continuous and not val:  # button released: momentary buttons switch their own LED off,
            for s in self.strips:      # so put the mute light back
                if s.mute_key == key:
                    s.feedback()
            return
        if self.learning:
            s, what = self.learning
            if what == "fader" and not continuous or what == "mute" and (kind == "pb" or not val):
                return
            for o in self.strips:  # one control drives one channel
                if what == "fader" and o.fader_key == key: o.fader_key = None
                if what == "mute" and o.mute_key == key: o.mute_key = None
            self.ignored.discard(key)
            setattr(s, what + "_key", key)
            self.learning = None
            for o in self.strips:
                o.labels()
            s.feedback()
            self.save()
            return
        if not self.profile and continuous and key not in self.ignored \
                and not any(key in (s.fader_key, s.mute_key) for s in self.strips):
            s = self.add_strip({"name": f"Channel {len(self.strips) + 1}", "fader": key})  # one per knob
            self.on_streams({})
        for s in self.strips:
            if key not in (s.fader_key, s.mute_key):
                continue
            if not s.active:
                s.feedback()  # disabled channels ignore their controls (and stay dark)
                continue
            if s.fader_key == key:
                s.scale.set_value(round(val * 100))
            elif val:
                s.mute.set_active(not s.mute.get_active())
            else:
                s.feedback()  # CC button released
                continue
            self.osd.show_level(s.name.get_text(), ", ".join(s.names), s.scale.get_value(), s.mute.get_active())

    def save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self.write)

    def write(self):
        self._save_id = None
        self.store()
        self.cfg["ignored"] = sorted(self.ignored)
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        with open(CFG + ".tmp", "w") as f:  # write-then-rename, so a crash never leaves half a file
            json.dump(self.cfg, f, indent=1)
        os.replace(CFG + ".tmp", CFG)


if __name__ == "__main__":  # single instance: launching again just shows the running mixer
    if missing := [c for c in ("pactl", "aseqdump", "aconnect", "stdbuf") if not shutil.which(c)]:
        msg = f"MIDI Mixer needs {', '.join(missing)}. Install pulseaudio-utils (pactl) and alsa-utils."
        print(msg, file=sys.stderr)
        Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=msg).run()
        sys.exit(1)
    app = Gtk.Application(application_id="io.github.midi_mixer_linux")
    app.connect("activate", lambda a: a.win.present() if hasattr(a, "win") else
                setattr(a, "win", App(a, "--hidden" in sys.argv)))
    app.run(sys.argv[:1])
