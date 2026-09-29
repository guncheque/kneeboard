# SPDX-License-Identifier: MIT
# Copyright (c) 2026 GracelessDev
"""Joystick input for the kneeboard.

Two backends produce the same normalized InputEvent stream:

  evdev  Linux. Kernel names: BTN_TRIGGER_HAPPY1, ABS_HAT0Y:-1 ...
  sdl    Windows/macOS (and Linux if asked). SDL2 names: BUTTON_5, HAT0_UP ...

Both read without grabbing the device, so the game still sees every input.
A shared Bindings dispatcher turns events into kneeboard actions.
"""
from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import evdev
    from evdev import ecodes
except ImportError:
    evdev = None
    ecodes = None

log = logging.getLogger("kneeboard")

SDL_SPEC = re.compile(r"^(BUTTON_\d+|HAT\d+_(UP|DOWN|LEFT|RIGHT))$")
SDL_HAT_BITS = [(1, "UP"), (2, "RIGHT"), (4, "DOWN"), (8, "LEFT")]
RELEASE_ALL = "*"  # pseudo-spec: device went away, drop anything it was holding


@dataclass
class InputEvent:
    dev: str            # device name
    path: str           # evdev node, "" for SDL
    spec: str           # binding string, or RELEASE_ALL
    down: bool
    num: int | None = None  # 1-based button position, for display


Emit = Callable[[InputEvent], None]


# --------------------------------------------------------------------------
# evdev backend (Linux)
# --------------------------------------------------------------------------

def code_name(etype: int, code: int) -> str:
    table = ecodes.ABS if etype == ecodes.EV_ABS else {**ecodes.KEY, **ecodes.BTN}
    name = table.get(code, str(code))
    # aliased codes: prefer the specific name (BTN_TRIGGER over BTN_JOYSTICK,
    # BTN_TRIGGER_HAPPY1 over BTN_TRIGGER_HAPPY)
    return name[-1] if isinstance(name, (list, tuple)) else name


def valid_evdev_spec(spec: str) -> bool:
    if ecodes is None:
        return False
    name, _, value = spec.partition(":")
    if name.isdigit():
        return True
    if name not in ecodes.ecodes:
        return False
    if name.startswith("ABS_"):
        return name.startswith("ABS_HAT") and value in ("-1", "1")
    return value == ""


def valid_spec(spec: str) -> bool:
    return bool(SDL_SPEC.match(spec)) or valid_evdev_spec(spec)


def looks_like_joystick(dev) -> bool:
    keys = dev.capabilities().get(ecodes.EV_KEY, [])
    return any(0x120 <= k <= 0x13f or k >= 0x2c0 for k in keys)


class EvdevSource:
    name = "evdev"

    def __init__(self):
        self.devices: dict[str, str] = {}  # path -> name

    async def run(self, emit: Emit) -> None:
        readers: dict[str, asyncio.Task] = {}
        try:
            while True:  # hotplug: look for new sticks every few seconds
                for path in evdev.list_devices():
                    if path in readers and not readers[path].done():
                        continue
                    try:
                        dev = evdev.InputDevice(path)
                    except OSError:
                        continue
                    if not looks_like_joystick(dev):
                        dev.close()
                        continue
                    self.devices[path] = dev.name
                    log.info("joystick: %s (%s)", dev.name, path)
                    readers[path] = asyncio.create_task(self._read(dev, emit))
                await asyncio.sleep(3)
        finally:
            for t in readers.values():
                t.cancel()

    async def _read(self, dev, emit: Emit) -> None:
        keys = sorted(dev.capabilities().get(ecodes.EV_KEY, []))
        hat_last: dict[int, int] = {}
        name, path = dev.name, dev.path
        try:
            async for ev in dev.async_read_loop():
                if ev.type == ecodes.EV_KEY and ev.value in (0, 1):
                    num = keys.index(ev.code) + 1 if ev.code in keys else None
                    emit(InputEvent(name, path, code_name(ev.type, ev.code), ev.value == 1, num))
                elif ev.type == ecodes.EV_ABS:
                    axis = code_name(ev.type, ev.code)
                    if not axis.startswith("ABS_HAT"):
                        continue
                    prev = hat_last.get(ev.code, 0)
                    hat_last[ev.code] = ev.value
                    if prev == ev.value:
                        continue
                    if prev:
                        emit(InputEvent(name, path, f"{axis}:{prev}", False))
                    if ev.value:
                        emit(InputEvent(name, path, f"{axis}:{ev.value}", True))
        except OSError as e:
            log.warning("joystick %s disconnected (%s)", name, e)
        finally:
            emit(InputEvent(name, path, RELEASE_ALL, False))
            self.devices.pop(path, None)
            try:
                dev.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# SDL2 backend (Windows, macOS)
# --------------------------------------------------------------------------

class SDLSource:
    """SDL runs on its own thread (it wants init and polling on one thread)."""
    name = "sdl"

    def __init__(self, test_hook: Callable | None = None):
        self.devices: dict[int, str] = {}  # instance id -> name
        self._test_hook = test_hook

    async def run(self, emit: Emit) -> None:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        failed = asyncio.Event()

        def push(ev: InputEvent) -> None:
            loop.call_soon_threadsafe(emit, ev)

        def fail() -> None:
            loop.call_soon_threadsafe(failed.set)

        def guarded():
            try:
                self._thread(push, stop, fail)
            except Exception:
                log.exception("SDL input thread crashed")
                fail()

        thread = threading.Thread(target=guarded, name="sdl-input", daemon=True)
        thread.start()
        try:
            await failed.wait()  # only returns if SDL fails
            log.error("joystick input stopped; kneeboard still works from the tablet")
        finally:
            stop.set()

    def _thread(self, push, stop: threading.Event, fail) -> None:
        # read sticks even when the kneeboard console isn't the focused window
        import sdl2

        sdl2.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        if sdl2.SDL_Init(sdl2.SDL_INIT_JOYSTICK) != 0:
            log.error("SDL joystick init failed: %s", sdl2.SDL_GetError().decode(errors="replace"))
            fail()
            return
        opened: dict[int, tuple] = {}
        hats: dict[tuple[int, int], int] = {}
        ev = sdl2.SDL_Event()
        try:
            while not stop.is_set():
                if self._test_hook:
                    self._test_hook(sdl2)
                while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                    t = ev.type
                    if t == sdl2.SDL_JOYDEVICEADDED:
                        joy = sdl2.SDL_JoystickOpen(ev.jdevice.which)
                        if not joy:
                            continue
                        iid = sdl2.SDL_JoystickInstanceID(joy)
                        name = (sdl2.SDL_JoystickName(joy) or b"joystick").decode(errors="replace")
                        opened[iid] = (joy, name)
                        self.devices[iid] = name
                        log.info("joystick: %s", name)
                    elif t == sdl2.SDL_JOYDEVICEREMOVED:
                        iid = ev.jdevice.which
                        entry = opened.pop(iid, None)
                        self.devices.pop(iid, None)
                        if entry:
                            sdl2.SDL_JoystickClose(entry[0])
                            log.warning("joystick %s disconnected", entry[1])
                            push(InputEvent(entry[1], "", RELEASE_ALL, False))
                        for key in [k for k in hats if k[0] == iid]:
                            del hats[key]
                    elif t in (sdl2.SDL_JOYBUTTONDOWN, sdl2.SDL_JOYBUTTONUP):
                        b = ev.jbutton
                        name = opened.get(b.which, (None, "joystick"))[1]
                        push(InputEvent(name, "", f"BUTTON_{b.button + 1}",
                                        t == sdl2.SDL_JOYBUTTONDOWN, b.button + 1))
                    elif t == sdl2.SDL_JOYHATMOTION:
                        h = ev.jhat
                        name = opened.get(h.which, (None, "joystick"))[1]
                        key = (h.which, h.hat)
                        prev = hats.get(key, 0)
                        hats[key] = h.value
                        for bit, direction in SDL_HAT_BITS:  # diagonals = two directions
                            if (prev ^ h.value) & bit:
                                push(InputEvent(name, "", f"HAT{h.hat}_{direction}",
                                                bool(h.value & bit)))
                stop.wait(0.005)
        finally:
            for joy, _ in opened.values():
                sdl2.SDL_JoystickClose(joy)
            sdl2.SDL_Quit()


def sdl_available() -> bool:
    return importlib.util.find_spec("sdl2") is not None


def make_source(pref: str = "auto"):
    pref = (pref or "auto").lower()
    linux = sys.platform.startswith("linux")
    if pref == "evdev" or (pref == "auto" and linux and evdev is not None):
        if evdev is None:
            log.error("backend 'evdev' requested but python-evdev isn't installed")
            return None
        return EvdevSource()
    if pref in ("sdl", "auto"):
        if sdl_available():
            return SDLSource()
        log.error("no joystick backend: install pysdl2 and pysdl2-dll%s",
                  " (or python-evdev on Linux)" if linux else "")
        return None
    log.error("unknown input backend %r (use auto, evdev or sdl)", pref)
    return None


# --------------------------------------------------------------------------
# Bindings
# --------------------------------------------------------------------------

class Bindings:
    def __init__(self, blocks: list[dict], actions: set[str]):
        self.blocks = []
        for cfg in blocks:
            match = cfg.get("match", "")
            binds: dict[tuple[bool, str], str] = {}
            for spec, action in cfg.get("bindings", {}).items():
                mod = spec.startswith("mod+")
                bare = spec.removeprefix("mod+")
                if action not in actions:
                    log.error("device %r: unknown action %r for %s", match, action, spec)
                elif not valid_spec(bare):
                    log.error("device %r: bad binding %r (wrong backend's name, or a typo?)",
                              match, spec)
                else:
                    binds[(mod, bare)] = action
            modifier = cfg.get("modifier")
            if modifier and not valid_spec(modifier):
                log.error("device %r: bad modifier %r", match, modifier)
                modifier = None
            target = os.path.realpath(match) if match.startswith("/dev/") else None
            self.blocks.append({"match": match.lower(), "path": target, "modifier": modifier,
                                "binds": binds, "labels": cfg.get("labels", {})})
        self.held: set[tuple[str, str]] = set()  # (device, spec) modifiers held down

    def _matches(self, block: dict, ev: InputEvent) -> bool:
        if block["path"]:
            return ev.path and os.path.realpath(ev.path) == block["path"]
        return block["match"] in ev.dev.lower()

    def handle(self, ev: InputEvent) -> list[str]:
        if ev.spec == RELEASE_ALL:
            self.held = {h for h in self.held if h[0] != ev.dev}
            return []
        blocks = [b for b in self.blocks if self._matches(b, ev)]
        if any(b["modifier"] == ev.spec for b in blocks):
            (self.held.add if ev.down else self.held.discard)((ev.dev, ev.spec))
            return []
        if not ev.down:
            return []
        mod = bool(self.held)  # a modifier on one device shifts all of them
        out = []
        for b in blocks:
            action = b["binds"].get((mod, ev.spec))
            if action:
                log.debug("%s -> %s", b["labels"].get(ev.spec, ev.spec), action)
                out.append(action)
        return out


# --------------------------------------------------------------------------
# Interactive tools: --learn and --bind
# --------------------------------------------------------------------------

def describe(spec: str, num: int | None) -> str:
    return f"{spec}  (button {num})" if num and not spec.startswith("BUTTON_") else spec


def read_labels(path: Path | None) -> dict[tuple[str, str], str]:
    """(device match, bare spec) -> label, from an existing config."""
    if not path or not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {(d.get("match", ""), spec.removeprefix("mod+")): label
            for d in cfg.get("device", []) for spec, label in d.get("labels", {}).items()}


def label_for(labels: dict, dev_name: str, spec: str) -> str | None:
    for (match, s), label in labels.items():
        if s == spec and match.lower() in dev_name.lower():
            return label
    return None


async def _start(source) -> tuple[asyncio.Queue, asyncio.Task]:
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(source.run(q.put_nowait))
    await asyncio.sleep(1.5)  # let devices enumerate
    return q, task


def _stdin_lines(q: asyncio.Queue) -> None:
    """Feed typed lines into q from a daemon thread (works on Windows consoles too)."""
    loop = asyncio.get_running_loop()

    def reader():
        for line in sys.stdin:
            loop.call_soon_threadsafe(q.put_nowait,
                                      InputEvent("<enter>", "", line.rstrip("\r\n"), True))
        loop.call_soon_threadsafe(q.put_nowait, InputEvent("<eof>", "", "", True))

    threading.Thread(target=reader, name="stdin", daemon=True).start()


async def learn(source, config_path: Path | None) -> None:
    """Print the binding string for every button/hat you press."""
    q, task = await _start(source)
    if not source.devices:
        print("No joysticks found yet (plug one in, or see README for permissions). Listening anyway.")
    labels = read_labels(config_path)
    print("Press buttons or hats. Ctrl+C to stop.\n")
    try:
        while True:
            ev = await q.get()
            if ev.down and ev.spec != RELEASE_ALL:
                label = label_for(labels, ev.dev, ev.spec)
                tail = f"  \"{label}\"" if label else ""
                print(f"{ev.dev!r:40}  {describe(json.dumps(ev.spec), ev.num)}{tail}")
    finally:
        task.cancel()


WIZARD_ACTIONS = [
    ("next_page", "Next page"),
    ("prev_page", "Previous page"),
    ("next_doc", "Next document"),
    ("prev_doc", "Previous document"),
    ("toggle_night", "Toggle night mode"),
    ("first_page", "Jump to first page"),
    ("toggle_fit", "Toggle fit page / fit width"),
]


def toml_str(s: str) -> str:
    return json.dumps(s)  # JSON strings are valid TOML basic strings


def render_config(base: dict, modifier: dict | None, binds: list[dict]) -> str:
    """base: host/port/docs_dir/backend. modifier/binds: {dev, spec, label, [action]}."""
    lines = [
        "# Written by the kneeboard --bind wizard. Safe to hand-edit; see config.example.toml.",
        f"host = {toml_str(base['host'])}",
        f"port = {int(base['port'])}",
        f"docs_dir = {toml_str(str(base['docs_dir']))}",
        f"backend = {toml_str(base.get('backend', 'auto'))}",
    ]
    devices = list(dict.fromkeys([b["dev"] for b in binds] + ([modifier["dev"]] if modifier else [])))
    for dev in devices:
        lines += ["", "[[device]]", f"match = {toml_str(dev)}"]
        labels = {}
        if modifier and modifier["dev"] == dev:
            comment = f"  # {modifier['label']}" if modifier["label"] else ""
            lines.append(f"modifier = {toml_str(modifier['spec'])}{comment}")
            if modifier["label"]:
                labels[modifier["spec"]] = modifier["label"]
        lines.append("[device.bindings]")
        for b in binds:
            if b["dev"] != dev:
                continue
            key = f"mod+{b['spec']}" if modifier else b["spec"]
            comment = f"  # {b['label']}" if b["label"] else ""
            lines.append(f"{toml_str(key)} = {toml_str(b['action'])}{comment}")
            if b["label"]:
                labels[b["spec"]] = b["label"]
        if labels:
            lines.append("[device.labels]")
            lines += [f"{toml_str(k)} = {toml_str(v)}" for k, v in labels.items()]
    return "\n".join(lines) + "\n"


async def bind_wizard(source, path: Path, defaults: dict) -> None:
    q, task = await _start(source)
    _stdin_lines(q)
    old_labels = read_labels(path)

    def drain():
        while not q.empty():
            q.get_nowait()

    async def next_event() -> InputEvent:
        while True:
            ev = await q.get()
            if ev.dev == "<eof>":
                raise KeyboardInterrupt
            if ev.dev == "<enter>" or (ev.down and ev.spec != RELEASE_ALL):
                return ev

    async def ask_label(dev: str, spec: str) -> str:
        known = label_for(old_labels, dev, spec) or ""
        hint = f" [{known}]" if known else ""
        print(f"  label, e.g. 'nub press'{hint} (Enter to {'keep' if known else 'skip'}): ",
              end="", flush=True)
        while True:  # ignore stick input while typing
            ev = await next_event()
            if ev.dev == "<enter>":
                return ev.spec.strip() or known

    async def capture(prompt: str):
        await asyncio.sleep(0.3)  # let the previous press settle
        drain()
        print(f"\n{prompt}\n  press an input, or Enter to skip: ", end="", flush=True)
        ev = await next_event()
        if ev.dev == "<enter>":
            print("  skipped")
            return None
        await asyncio.sleep(0.5)  # swallow bounce / the other hat axis
        drain()
        print(f"{describe(ev.spec, ev.num)}  on {ev.dev}")
        return {"dev": ev.dev, "spec": ev.spec}

    try:
        print(f"Kneeboard binding wizard ({source.name} input). Devices:")
        for name in source.devices.values() or ["(none found yet; plug a stick in and press a button)"]:
            print(f"  {name}")
        print("\nAfter each press you can type a label for it (the name on the grip), "
              "which is saved in the config and shown by --learn.")

        modifier = None
        while True:
            got = await capture("MODIFIER: a button you hold to shift into kneeboard controls "
                                "(recommended if you're reusing buttons the game already uses)")
            if got is None or not ("HAT" in got["spec"]):
                modifier = got
                break
            print("  a modifier has to be a button, not a hat direction; try again")
        if modifier:
            modifier["label"] = await ask_label(modifier["dev"], modifier["spec"])

        binds: list[dict] = []
        for action, label in WIZARD_ACTIONS:
            while True:
                got = await capture(label.upper() + ("  (while holding the modifier)" if modifier else ""))
                if got is None:
                    break
                same = lambda b: (b["dev"], b["spec"]) == (got["dev"], got["spec"])
                clash = [b["action"] for b in binds if same(b)]
                if modifier and same(modifier):
                    print("  that's the modifier; pick a different input")
                elif clash:
                    print(f"  already bound to {clash[0]}; pick a different input")
                else:
                    got["action"] = action
                    got["label"] = await ask_label(got["dev"], got["spec"])
                    binds.append(got)
                    break
    finally:
        task.cancel()

    if not binds:
        print("\nNothing bound; config left unchanged.")
        return

    base = dict(defaults)
    if path.is_file():
        with open(path, "rb") as f:
            old = tomllib.load(f)
        base.update({k: old[k] for k in ("host", "port", "docs_dir", "backend") if k in old})
        backup = path.with_suffix(".toml.bak")
        path.replace(backup)
        print(f"\nOld config backed up to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(base, modifier, binds), encoding="utf-8")
    print(f"Wrote {path}")
    if modifier:
        name = modifier["label"] or modifier["spec"]
        print(f"\nThe game still sees every press. In DCS, go to Controls > Modifiers and add\n"
              f"{name} as a modifier there too (press it in the Add dialog), and leave those\n"
              f"modifier combos unbound, so DCS ignores them while the kneeboard uses them.")
    else:
        print("\nNo modifier: make sure the game has nothing bound to the inputs you just picked.")
