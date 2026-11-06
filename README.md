# MIDI Mixer for Linux

Control per-application volume and mute with any MIDI controller. Works with PulseAudio and PipeWire.
It's one Python file with no pip dependencies.

## Install
```bash
curl -fsSL https://raw.githubusercontent.com/adamkahl/midi-mixer-linux/main/install.sh | bash
```
This puts MIDI Mixer in your app menu and starts it. It installs to `~/.local` without root, and only asks for
`sudo` if a system package is missing (GTK 3 for Python, `pactl`, `alsa-utils`; usually preinstalled on
Mint/Ubuntu). It supports apt, dnf, pacman and zypper. Run the same command again to update.

**Uninstall:** `curl -fsSL https://raw.githubusercontent.com/adamkahl/midi-mixer-linux/main/install.sh | bash -s -- --uninstall`

**Run without installing:** `git clone https://github.com/adamkahl/midi-mixer-linux && ./midi-mixer-linux/midimixer.py`

## Usage
- **Assign ▾**: choose what a channel controls: Master output, Microphone, Unassigned apps,
  or any app that is playing audio. A channel can control several targets.
- **Knob: —**: click it, then move a knob or fader on your controller to bind it (CC or pitch bend).
- **Button: —**: click it, then press a button to bind it as a mute toggle (a note or CC).
- **+** in the title bar adds a channel and **×** removes one. Settings save to `~/.config/midi-mixer-linux/config.json`.
- New audio streams from an assigned app get that channel's volume and mute state.
- MIDI devices are connected automatically, including ones plugged in later.
- **Feedback to the controller:** mute buttons light up when muted, and knob LED rings or motor faders
  follow the real volume, including changes made in other apps. If a button doesn't light, set the
  controller's LED mode to "external" or "host" in its editor (for example, the KORG nanoKONTROL2).

## Controllers
- **Behringer X-Touch Mini:** detected as soon as it's plugged in (the title bar shows "X-Touch Mini").
  You get exactly 9 channels, one per control: knobs 1–8, each muted with the top-row button above it,
  and the fader (Master by default), muted with the second-from-right bottom button.
  The **bottom-right button** shows the mixer on top of whatever is on screen (even a fullscreen game),
  so you can assign the game to a channel; press it again to hide the mixer. Its light shows when it's open.
  **Every button is programmable:** click the gamepad button in the title bar (**Controller buttons**),
  click a button in the picture, or just press it on the X-Touch, and choose what it does:
  mute any channel, show / hide the mixer, play / pause, next or previous track (Brave, Spotify, VLC and
  other media players), mute the microphone, or run any command or script. Lights show mute state,
  whether the mixer is open, and whether something is playing. You can rename channels and
  choose their apps, but you can't add, remove or re-bind them. The mixer switches the X-Touch to
  standard mode (MC MODE off) for you. Leave its buttons in momentary mode (the factory setting).
- **Any other controller:** moving a knob or fader that isn't bound yet adds a channel for it.
  Channels can be added, removed and bound with **Knob:** / **Button:**.
- **Channels not in use are disabled:** if none of a channel's apps is playing audio (or it has none),
  it's greyed out, ignores its knob, and its ring and button light stay off. It comes back as soon as
  the app starts playing. Master and Microphone are always in use.
- Assigning an app to a channel picks up that app's current volume instead of changing it.
- **Apps in the Assign list:** every app that has ever played audio is remembered, so it stays in the
  list when it's quiet (browsers like Brave only show up while a tab is making sound). For an app that
  hasn't played anything yet, type its name (e.g. `brave`) in the box at the bottom of the list.
  When an assigned app starts playing, its channel switches on and takes control straight away.
- Assignments, names and levels are saved as you change them, and also on logout or shutdown.
- Unplugging and replugging is handled automatically.

## Tray icon and starting on login
- Closing the window hides MIDI Mixer to the system tray, and it keeps running. Click the tray icon to show
  or hide the window. Right-click it for **Start on login** and **Quit**.
- **Start on login** adds `~/.config/autostart/midi-mixer.desktop`, which starts the mixer hidden in the tray.
- Only one copy runs at a time. Launching it again opens the window that's already running.
- Without a system tray (e.g. stock GNOME, or remote desktop), launch the mixer again (`midi-mixer` or the app menu) to show the window.
  Quit it from the tray menu, or with `pkill -f midimixer.py`.

## Troubleshooting
- **"No MIDI connection"** in the title bar: the window still controls volumes. MIDI is missing because Linux gives MIDI and sound devices
  only to the session at the computer itself, so this appears in remote desktop (xrdp) sessions. Log
  in locally, or add yourself to the `audio` group and log in again: `sudo usermod -aG audio $USER`.
  The mixer keeps running and connects as soon as MIDI becomes available.

## License
MIT
