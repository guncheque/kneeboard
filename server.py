#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 GracelessDev
"""Kneeboard daemon.

Serves PDFs/images to a tablet browser and maps joystick buttons (read
non-exclusively via evdev on Linux or SDL2 elsewhere, so the game still sees
them) to page actions.
The server owns the current doc/page, so the stick and the tablet's own
taps stay in sync, and any number of tablets can follow along.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
import tomllib
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from aiohttp import WSMsgType, web

import inputs

log = logging.getLogger("kneeboard")

# PyInstaller unpacks bundled data under sys._MEIPASS
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CLIENT_DIR = HERE / "client"
PDF_EXT = {".pdf"}
# pdfium is not thread-safe: every call into it goes through this lock
PDFIUM_LOCK = threading.Lock()
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
ACTIONS = {
    "next_page", "prev_page", "first_page",
    "next_doc", "prev_doc",
    "toggle_night", "toggle_fit", "rescan",
}
DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8420,
    "docs_dir": "~/Documents/kneeboard",
    "backend": "auto",  # auto | evdev | sdl
    "device": [],
}


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------

class Library:
    """Every PDF/image under docs_dir is one doc. Subfolders become name prefixes."""

    def __init__(self, root: Path):
        self.root = root
        self.docs: list[dict] = []
        self.signature: tuple = ()
        self.rescan()

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        files = [p for p in self.root.rglob("*")
                 if p.is_file() and p.suffix.lower() in PDF_EXT | IMG_EXT]
        return sorted(files, key=lambda p: str(p.relative_to(self.root)).lower())

    def current_signature(self) -> tuple:
        out = []
        for p in self._files():
            try:
                out.append((str(p), p.stat().st_mtime))
            except OSError:
                pass
        return tuple(out)

    def rescan(self) -> None:
        docs = []
        for p in self._files():
            is_pdf = p.suffix.lower() in PDF_EXT
            pages = 1
            if is_pdf:
                try:
                    with PDFIUM_LOCK:
                        pdf = pdfium.PdfDocument(p)
                        pages = len(pdf)
                        pdf.close()
                except Exception as e:  # corrupt / half-copied file
                    log.warning("skipping %s: %s", p.name, e)
                    continue
            if pages < 1:
                continue
            docs.append({
                "id": len(docs),
                "name": str(p.relative_to(self.root).with_suffix("")),
                "path": p,
                "pages": pages,
                "kind": "pdf" if is_pdf else "image",
                "v": int(p.stat().st_mtime),
            })
        self.docs = docs
        self.signature = self.current_signature()
        log.info("library: %d docs in %s", len(docs), self.root)

    def public(self) -> list[dict]:
        return [{k: d[k] for k in ("id", "name", "pages", "kind", "v")} for d in self.docs]


@lru_cache(maxsize=96)
def render_pdf_page(path: str, version: int, page: int, width: int) -> bytes:
    # version is only part of the cache key, so an edited file re-renders
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            pg = pdf[page]
            image = pg.render(scale=width / pg.get_width()).to_pil()
        finally:
            pdf.close()
    buf = BytesIO()
    image.save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------

class Kneeboard:
    def __init__(self, library: Library):
        self.lib = library
        self.doc = 0
        self.page_by_doc: dict[str, int] = {}  # remember page per doc (by name)
        self.night = False
        self.fit = "page"  # "page" or "width"
        self.clients: set[web.WebSocketResponse] = set()

    # -- state helpers --
    @property
    def page(self) -> int:
        if not self.lib.docs:
            return 0
        return self.page_by_doc.get(self.lib.docs[self.doc]["name"], 0)

    @page.setter
    def page(self, value: int) -> None:
        if self.lib.docs:
            self.page_by_doc[self.lib.docs[self.doc]["name"]] = value

    def clamp(self) -> None:
        n = len(self.lib.docs)
        if n == 0:
            self.doc = 0
            return
        self.doc = max(0, min(self.doc, n - 1))
        pages = self.lib.docs[self.doc]["pages"]
        self.page = max(0, min(self.page, pages - 1))

    def state_msg(self) -> dict:
        return {"type": "state", "doc": self.doc, "page": self.page,
                "night": self.night, "fit": self.fit}

    def library_msg(self) -> dict:
        return {"type": "library", "docs": self.lib.public()}

    # -- actions --
    async def apply(self, action: str, **args) -> None:
        if action == "rescan":
            await self.rescan()
            return
        if action == "goto":
            if "doc" in args:
                self.doc = int(args["doc"])
                self.clamp()
            if "page" in args:
                self.page = int(args["page"])
        elif action not in ACTIONS:
            log.warning("unknown action %r", action)
            return
        n = len(self.lib.docs)
        if action == "toggle_night":
            self.night = not self.night
        elif action == "toggle_fit":
            self.fit = "width" if self.fit == "page" else "page"
        elif n:
            pages = self.lib.docs[self.doc]["pages"]
            if action == "next_page" and self.page < pages - 1:
                self.page += 1
            elif action == "prev_page" and self.page > 0:
                self.page -= 1
            elif action == "first_page":
                self.page = 0
            elif action == "next_doc":
                self.doc = (self.doc + 1) % n
            elif action == "prev_doc":
                self.doc = (self.doc - 1) % n
        self.clamp()
        log.debug("action %s -> doc=%d page=%d", action, self.doc, self.page)
        await self.broadcast(self.state_msg())

    async def rescan(self) -> None:
        current = self.lib.docs[self.doc]["name"] if self.lib.docs else None
        await asyncio.get_running_loop().run_in_executor(None, self.lib.rescan)
        # keep the same doc selected if it still exists
        for d in self.lib.docs:
            if d["name"] == current:
                self.doc = d["id"]
                break
        self.clamp()
        await self.broadcast(self.library_msg())
        await self.broadcast(self.state_msg())

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def make_app(kb: Kneeboard) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_req):
        return web.FileResponse(CLIENT_DIR / "index.html")

    @routes.get("/ws")
    async def ws_handler(req):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(req)
        kb.clients.add(ws)
        log.info("tablet connected (%s), %d total", req.remote, len(kb.clients))
        await ws.send_json(kb.library_msg())
        await ws.send_json(kb.state_msg())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                    action = data.pop("action")
                except (ValueError, KeyError, AttributeError):
                    continue
                await kb.apply(action, **data)
        finally:
            kb.clients.discard(ws)
            log.info("tablet disconnected, %d left", len(kb.clients))
        return ws

    @routes.get("/page/{doc:\\d+}/{page:\\d+}")
    async def page(req):
        di, pi = int(req.match_info["doc"]), int(req.match_info["page"])
        if di >= len(kb.lib.docs):
            raise web.HTTPNotFound()
        d = kb.lib.docs[di]
        if pi >= d["pages"]:
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "public, max-age=86400"}  # URL carries ?v=mtime
        if d["kind"] == "image":
            return web.FileResponse(d["path"], headers=headers)
        try:
            width = int(req.query.get("w", 1600))
        except ValueError:
            width = 1600
        width = max(400, min(3200, -(-width // 200) * 200))  # round up to 200px steps
        png = await asyncio.get_running_loop().run_in_executor(
            None, render_pdf_page, str(d["path"]), d["v"], pi, width)
        return web.Response(body=png, content_type="image/png", headers=headers)

    @routes.post("/api/action/{name}")
    async def action(req):
        # handy for testing or for binding actions from other tools:
        #   curl -X POST http://localhost:8420/api/action/next_page
        name = req.match_info["name"]
        if name not in ACTIONS:
            raise web.HTTPBadRequest(text=f"unknown action {name}")
        await kb.apply(name)
        return web.json_response(kb.state_msg())

    @routes.get("/api/state")
    async def state(_req):
        return web.json_response({**kb.state_msg(), "docs": kb.lib.public()})

    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static/", CLIENT_DIR)
    return app


async def watch_library(kb: Kneeboard, interval: float = 3.0) -> None:
    """Pick up files dropped into docs_dir without a restart."""
    while True:
        await asyncio.sleep(interval)
        try:
            sig = await asyncio.get_running_loop().run_in_executor(
                None, kb.lib.current_signature)
            if sig != kb.lib.signature:
                await kb.rescan()
        except Exception:
            log.exception("library watch failed")


async def watch_inputs(kb: Kneeboard, source, bindings: "inputs.Bindings") -> None:
    def emit(ev):
        for action in bindings.handle(ev):
            asyncio.create_task(kb.apply(action))
    await source.run(emit)


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------

def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "kneeboard" / "config.toml"


def load_config(path: Path | None) -> dict:
    candidates = [path] if path else [Path.cwd() / "config.toml", default_config_path()]
    for p in candidates:
        if p and p.is_file():
            with open(p, "rb") as f:
                log.info("config: %s", p)
                return {**DEFAULT_CONFIG, **tomllib.load(f)}
    if path:
        sys.exit(f"config not found: {path}")
    log.info("no config found (run with --bind to set up buttons); using defaults")
    return dict(DEFAULT_CONFIG)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:  # odd console encodings; the URL is printed anyway
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Joystick-controlled tablet kneeboard")
    ap.add_argument("-c", "--config", type=Path)
    ap.add_argument("--list-devices", action="store_true", help="list joysticks and exit")
    ap.add_argument("--learn", action="store_true", help="print binding names for pressed buttons")
    ap.add_argument("--bind", action="store_true",
                    help="guided setup: press an input for each action, writes the config")
    ap.add_argument("--backend", choices=["auto", "evdev", "sdl"],
                    help="joystick backend (default: from config, else auto)")
    ap.add_argument("--no-joystick", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    config_path = args.config or default_config_path()
    tools = args.list_devices or args.learn or args.bind
    cfg = load_config(args.config) if not tools or config_path.is_file() else dict(DEFAULT_CONFIG)
    backend = args.backend or cfg.get("backend", "auto")

    if tools:
        source = inputs.make_source(backend)
        if source is None:
            sys.exit(1)
        try:
            if args.list_devices:
                async def list_devices():
                    task = asyncio.create_task(source.run(lambda ev: None))
                    await asyncio.sleep(1.5)
                    task.cancel()
                    print(f"{source.name} joysticks:")
                    for key, name in source.devices.items():
                        print(f"  {name}" + (f"   ({key})" if isinstance(key, str) else ""))
                    if not source.devices:
                        print("  (none found)")
                asyncio.run(list_devices())
            elif args.learn:
                asyncio.run(inputs.learn(source, config_path))
            else:
                defaults = {**DEFAULT_CONFIG, "backend": backend}
                asyncio.run(inputs.bind_wizard(source, config_path, defaults))
        except KeyboardInterrupt:
            print("\nstopped" + (", config unchanged" if args.bind else ""))
        return

    docs_dir = Path(cfg["docs_dir"]).expanduser()
    docs_dir.mkdir(parents=True, exist_ok=True)
    kb = Kneeboard(Library(docs_dir))
    app = make_app(kb)

    async def start_background(app_):
        tasks = [asyncio.create_task(watch_library(kb))]
        if not args.no_joystick and cfg.get("device"):
            source = inputs.make_source(backend)
            if source:
                bindings = inputs.Bindings(cfg["device"], ACTIONS)
                log.info("input: %s backend, %d device block(s)", source.name, len(cfg["device"]))
                tasks.append(asyncio.create_task(watch_inputs(kb, source, bindings)))
        elif not cfg.get("device"):
            log.info("no buttons bound yet; run with --bind to set them up")
        app_["tasks"] = tasks
        url = f"http://{lan_ip()}:{cfg['port']}/"
        log.info("docs folder: %s", docs_dir)
        log.info("tablet URL: %s", url)
        print_qr(url)

    async def stop_background(app_):
        for t in app_["tasks"]:
            t.cancel()

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    web.run_app(app, host=cfg["host"], port=cfg["port"], print=None)


def run() -> None:
    """Entry point that keeps a double-clicked Windows console open on errors."""
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None) and getattr(sys, "frozen", False):
            print(e.code if isinstance(e.code, str) else "")
            input("Press Enter to close...")
        raise
    except Exception:
        if getattr(sys, "frozen", False):
            import traceback
            traceback.print_exc()
            input("Press Enter to close...")
        raise


if __name__ == "__main__":
    run()
