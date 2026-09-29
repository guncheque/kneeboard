# Kneeboard

A tablet kneeboard for flight sims, driven from your HOTAS. It runs a small Linux daemon on the PC that serves your charts and checklists to any tablet browser on the LAN. It maps joystick buttons to page actions, so you can flip pages without taking your hands off the stick.

- **Any tablet, no app install.** Android, iPad, or an old e-reader with a browser.
- **Joystick control** through evdev on Linux or SDL2 on Windows. It reads without grabbing the device, so the game (even under Wine/Proton) still sees every input.
- **Linux and Windows.** Windows builds are a plain folder with `kneeboard.exe`; no Python needed.
- **Modifier layers.** Kneeboard binds can sit behind a hold-to-shift button, so you can reuse buttons the game already uses. You also need to set that button as a modifier in DCS; see below.
- **Server-owned state.** The stick, tablet taps, and multiple tablets all stay in sync.
- **Drop-in docs.** PDFs and images in a folder, picked up live. Subfolders become groups (`Caucasus/Kobuleti`).
- **Night mode** (inverted, dimmed) and **fit-page / fit-width**, both bindable.

## Setup (CachyOS / Arch, fish)

```fish
cd ~/dev
git clone <repo-url> kneeboard
cd kneeboard
python -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p ~/.config/kneeboard ~/Documents/kneeboard
cp config.example.toml ~/.config/kneeboard/config.toml
```

### Joystick access

systemd usually grants the logged-in user access to joysticks already. Check:

```fish
.venv/bin/python server.py --list-devices
```

If your stick doesn't show up, add yourself to the `input` group and log out and back in:

```fish
sudo usermod -aG input $USER
```

### Bind your buttons

```fish
.venv/bin/python server.py --bind
```

The wizard asks for a modifier (optional), then each action in turn. Press the input you want, or hit Enter to skip. After each press you can type a label ("nub press", "pinky paddle"); labels are saved in the config, offered again next time you run the wizard, and shown by `--learn`. It writes `~/.config/kneeboard/config.toml` and keeps your old one as `config.toml.bak`.

**DCS still sees every press.** The kneeboard only reads inputs; it can't hide them from the game. So either:

- Bind the kneeboard to inputs DCS has nothing on (spare throttle buttons are ideal), or
- Use a modifier, *and* add the same button as a modifier in DCS (Controls → Modifiers), leaving those modifier+button combos unbound in DCS.

The raw names (`BTN_TRIGGER`, `BTN_TRIGGER_HAPPY1`...) are Linux's generic slots, assigned in order, and don't describe the physical button. On the Orion 2 F/A-18 grip, `BTN_TRIGGER` is the trim hat press, not the trigger. Labels are there so you don't have to remember that. Only the trim hat is a real hat (`ABS_HAT0X/Y`); the other hats report as plain buttons.

### Run

```fish
.venv/bin/python server.py
```

The log prints the tablet URL (e.g. `http://192.168.1.20:8420/`). Open it on the tablet. Tap the middle of the screen for the bar; its ⛶ button toggles fullscreen. For a permanent fullscreen launcher, use your browser's **Add to Home screen** (or **Install app**) and open the kneeboard from that icon. If the tablet can't connect, open the port:

```fish
sudo ufw allow 8420/tcp                              # if you use ufw
sudo firewall-cmd --add-port=8420/tcp --permanent    # if you use firewalld
sudo firewall-cmd --reload
```

### Start on login (optional)

```fish
mkdir -p ~/.config/systemd/user
cp kneeboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kneeboard
journalctl --user -u kneeboard -f
```

## Windows

1. Download `kneeboard-windows.zip` from the Releases page and unzip it anywhere.
2. Double-click **Set up buttons.bat** and follow the prompts.
3. Double-click **kneeboard.exe**. Allow it through the firewall when Windows asks (tick *Private networks*).
4. On the tablet, scan the QR code in the console window, or type the URL printed above it.

Put PDFs and images in `Documents\kneeboard`. Settings live in `%APPDATA%\kneeboard\config.toml`. **Test buttons.bat** prints the name of each button you press.

Windows shows "Windows protected your PC" the first time, because the exe isn't code-signed. Click **More info → Run anyway**.

On Windows, buttons are named by number (`BUTTON_5`, `HAT0_UP`), and labels work the same as on Linux.

### Building the Windows version

Push a tag and GitHub Actions builds the zip and attaches it to a release:

```fish
git tag v0.2.0
git push --tags
```

You can also run the **Build Windows** workflow by hand from the Actions tab and grab the zip from its artifacts. To build locally on a Windows machine:

```
pip install -r requirements.txt pyinstaller
python build.py
```

The result is in `dist\kneeboard`.

## Using it

| Where | Does |
|---|---|
| Tap left / right third | Previous / next page |
| Tap middle | Show the bar (doc list, fit, night, fullscreen) |
| Joystick binds | Anything in the action list below |

Actions: `next_page`, `prev_page`, `first_page`, `next_doc`, `prev_doc`, `toggle_night`, `toggle_fit`, `rescan`.

Each doc remembers its own page, so jumping to the airfield diagram and back returns you to where you were in the checklist.

You can also drive it from scripts or other tools:

```fish
curl -X POST http://localhost:8420/api/action/next_page
```

## Notes

- Screen wake lock only works over HTTPS. Over plain LAN http, set the tablet's screen timeout to "never" while flying, or put it behind a reverse proxy with a cert.
- PDF rendering uses pdfium (via pypdfium2), the same engine as Chrome's PDF viewer.

## Roadmap ideas

- DCS `Export.lua` bridge: live frequencies, waypoints, and the mission's own kneeboard pages
- Scratchpad page with stylus drawing, synced back to the PC
- Per-doc bookmarks bindable to buttons
- Rotate / crop per doc for scanned charts

## License

MIT © 2026 GracelessDev
