#!/usr/bin/env python3
"""
FexCraft - Launcher de Minecraft moderno (CustomTkinter + minecraft-launcher-lib)

Instalación:
    pip install customtkinter minecraft-launcher-lib psutil
    python fexcraft.py

Arquitectura (todo en un archivo, separado por capas):
    1. Theme / Paths ............ colores y rutas
    2. ConfigManager ............ persistencia JSON (cuentas, perfiles, ajustes)
    3. AccountManager ........... cuentas offline
    4. VersionService ........... versiones oficiales, snapshots, Fabric, Forge
    5. GameLauncher ............. instala + lanza Minecraft con subprocess
    6. UI (páginas) ............. Inicio, Instalaciones, Mods, Configuración
    7. ConsolePanel ............. consola retráctil con logs en tiempo real
    8. FexCraftApp .............. ventana principal que une todo

Regla de oro de hilos: la lógica pesada (red, instalación, juego) corre en hilos
y NUNCA toca widgets. Para actualizar la UI se usa app.ui(fn) que encola la
función y la ejecuta en el hilo principal de Tk.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional

import customtkinter as ctk

try:
    import minecraft_launcher_lib as mll
except ImportError:  # el launcher abre igual, pero avisa al intentar instalar/jugar
    mll = None

try:
    import psutil
except ImportError:
    psutil = None

# ───────────────────────────── 1. THEME / PATHS ─────────────────────────────
class Theme:
    BG = "#080c13"        # fondo profundo
    PANEL = "#0e1520"     # tarjetas
    PANEL2 = "#121c2a"    # botones secundarios / inputs elevados
    INPUT = "#090f18"
    BORDER = "#202d3e"
    TEXT = "#edf4ff"
    MUTED = "#8292a8"
    BLUE = "#1685ff"      # acento principal
    BLUE2 = "#55b5ff"     # acento claro
    BLUE_HOVER = "#0874e8"
    DANGER = "#ff647c"
    GREEN = "#48d9a0"
    FONT = "Segoe UI"     # cae a la fuente del sistema si no existe


APP_DIR = Path.home() / ".fexcraft"
MC_DIR = APP_DIR / "minecraft"          # carpeta .minecraft propia del launcher
MODS_DIR = MC_DIR / "mods"
CONFIG_FILE = APP_DIR / "config.json"
for _d in (APP_DIR, MC_DIR, MODS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=Theme.FONT, size=size, weight=weight)


def resource_path(name: str) -> str:
    """Ruta a un recurso, tanto en desarrollo como dentro del .exe de PyInstaller."""
    base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    return str(Path(base) / name)


def open_folder(path: Path):
    """Abre una carpeta en el explorador del sistema."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ───────────────────────────── 2. CONFIG ─────────────────────────────
@dataclass
class Account:
    username: str
    type: str = "offline"
    uuid: str = ""
    token: str = "0"                 # token de acceso ("0" para cuentas offline)
    refresh_token: str = ""


@dataclass
class Profile:
    name: str
    version_id: str                  # id real a lanzar (ej: fabric-loader-0.16-1.21)
    mc_version: str
    kind: str = "vanilla"            # vanilla | snapshot | fabric | forge


@dataclass
class Settings:
    ram_mb: int = 4096
    jvm_args: str = "-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200"
    java_path: str = ""              # vacío = "java" del PATH
    res_w: int = 1280
    res_h: int = 720


@dataclass
class Config:
    accounts: list = field(default_factory=list)
    active_account: int = -1
    profiles: list = field(default_factory=list)
    active_profile: int = -1
    settings: dict = field(default_factory=lambda: asdict(Settings()))


class ConfigManager:
    """Carga/guarda config.json. Cada cambio llama a save()."""

    def __init__(self):
        self.cfg = Config()
        self.load()

    def load(self):
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self.cfg = Config()

    def save(self):
        CONFIG_FILE.write_text(json.dumps(asdict(self.cfg), indent=2), encoding="utf-8")

    # Accesos tipados
    @property
    def settings(self) -> Settings:
        base = asdict(Settings())
        base.update({k: v for k, v in self.cfg.settings.items() if k in base})
        return Settings(**base)

    def set_settings(self, s: Settings):
        self.cfg.settings = asdict(s)
        self.save()

    @property
    def accounts(self) -> list[Account]:
        return [Account(**a) for a in self.cfg.accounts]

    @property
    def profiles(self) -> list[Profile]:
        return [Profile(**p) for p in self.cfg.profiles]

    def active_account(self) -> Optional[Account]:
        a = self.accounts
        return a[self.cfg.active_account] if 0 <= self.cfg.active_account < len(a) else None

    def active_profile(self) -> Optional[Profile]:
        p = self.profiles
        return p[self.cfg.active_profile] if 0 <= self.cfg.active_profile < len(p) else None


# ───────────────────────────── 3. CUENTAS ─────────────────────────────
class AccountManager:
    def __init__(self, config: ConfigManager):
        self.config = config

    @staticmethod
    def offline_uuid(name: str) -> str:
        """UUID v3 determinista igual al que usa Minecraft para 'OfflinePlayer:<nombre>'."""
        digest = hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()
        return str(uuid.UUID(bytes=digest, version=3))

    def add_offline(self, name: str) -> Account:
        name = name.strip()
        if not (3 <= len(name) <= 16) or not all(c.isalnum() or c == "_" for c in name):
            raise ValueError("El nombre debe tener 3-16 caracteres (letras, números o _).")
        acc = Account(username=name, type="offline", uuid=self.offline_uuid(name))
        self.config.cfg.accounts.append(asdict(acc))
        self.config.cfg.active_account = len(self.config.cfg.accounts) - 1
        self.config.save()
        return acc

    def remove(self, index: int):
        accs = self.config.cfg.accounts
        if 0 <= index < len(accs):
            accs.pop(index)
            self.config.cfg.active_account = min(self.config.cfg.active_account, len(accs) - 1)
            self.config.save()


# ───────────────────────────── 4. VERSIONES ─────────────────────────────
class VersionService:
    """Obtiene listas de versiones. Todas las llamadas son bloqueantes: usar en hilos."""

    def minecraft_versions(self) -> list[dict]:
        if mll is None:
            raise RuntimeError("Falta minecraft-launcher-lib.")
        return mll.utils.get_version_list()  # [{id, type, releaseTime, url}, ...]

    def fabric_game_versions(self) -> list[str]:
        return [v["version"] for v in mll.fabric.get_all_minecraft_versions() if v["stable"]]

    def forge_supported(self, mc_version: str) -> Optional[str]:
        return mll.forge.find_forge_version(mc_version)  # None si no hay Forge para esa versión

    def news(self) -> list[dict]:
        """Notas de parche oficiales de Mojang; si falla la red, devuelve contenido local."""
        try:
            req = urllib.request.Request("https://launchercontent.mojang.com/v2/javaPatchNotes.json",
                                         headers={"User-Agent": "FexCraft/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                entries = json.load(r).get("entries", [])[:6]
            return [{"title": e.get("title", "Novedad"), "tag": e.get("type", "release"),
                     "text": f"Versión {e.get('version', '?')}"} for e in entries]
        except Exception:
            return [
                {"title": "Bienvenido a FexCraft", "tag": "launcher",
                 "text": "Creá un perfil en Instalaciones y presioná JUGAR."},
                {"title": "Fabric y Forge", "tag": "mods",
                 "text": "Instalá loaders desde Instalaciones y gestioná tus mods en la pestaña Mods."},
            ]


# ───────────────────────────── 5. LANZADOR ─────────────────────────────
class GameLauncher:
    """
    Puente entre la UI y el proceso de Minecraft.

    Flujo de play():
      1) Asegura que la versión esté instalada (descarga librerías/assets/loader).
      2) Construye el comando con minecraft-launcher-lib (classpath, argumentos, auth).
      3) Lo ejecuta con subprocess.Popen y reenvía cada línea de stdout a la consola.
    """

    def __init__(self, config: ConfigManager, log: Callable[[str, str], None]):
        self.config = config
        self.log = log                   # log(texto, nivel)
        self.process: Optional[subprocess.Popen] = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # ---- instalación -------------------------------------------------
    def _callback(self, progress: Callable[[float, str], None]):
        state = {"max": 1, "cur": 0}
        return {
            "setStatus": lambda t: (self.log(t, "info"), progress(state["cur"] / max(state["max"], 1), t)),
            "setMax": lambda m: state.update(max=m),
            "setProgress": lambda p: (state.update(cur=p), progress(p / max(state["max"], 1), "")),
        }

    def install_profile(self, p: Profile, progress: Callable[[float, str], None]) -> str:
        """Instala el perfil y devuelve el version_id definitivo."""
        if mll is None:
            raise RuntimeError("Instalá minecraft-launcher-lib: pip install minecraft-launcher-lib")
        cb = self._callback(progress)
        mc = str(MC_DIR)
        if p.kind == "fabric":
            loader = mll.fabric.get_latest_loader_version()
            mll.fabric.install_fabric(p.mc_version, mc, loader_version=loader, callback=cb)
            return f"fabric-loader-{loader}-{p.mc_version}"
        if p.kind == "forge":
            fv = mll.forge.find_forge_version(p.mc_version)
            if not fv:
                raise RuntimeError(f"No hay Forge disponible para {p.mc_version}.")
            mll.forge.install_forge_version(fv, mc, callback=cb)
            return mll.forge.forge_to_installed_version(fv)
        mll.install.install_minecraft_version(p.mc_version, mc, callback=cb)
        return p.mc_version

    # ---- lanzamiento -------------------------------------------------
    def build_command(self, p: Profile, acc: Account) -> list[str]:
        s = self.config.settings
        jvm = [f"-Xmx{s.ram_mb}M", f"-Xms{min(s.ram_mb, 1024)}M"] + shlex.split(s.jvm_args)
        options = {
            "username": acc.username,
            "uuid": acc.uuid,
            "token": acc.token,
            "jvmArguments": jvm,
            "customResolution": True,
            "resolutionWidth": str(s.res_w),
            "resolutionHeight": str(s.res_h),
        }
        if s.java_path:
            options["executablePath"] = s.java_path
        return mll.command.get_minecraft_command(p.version_id, str(MC_DIR), options)

    def play(self, p: Profile, acc: Account, progress: Callable[[float, str], None],
             on_state: Callable[[str], None]):
        """Ejecutar SIEMPRE en un hilo (threading.Thread)."""
        try:
            on_state("installing")
            self.log(f"== Preparando {p.name} ({p.version_id}) ==", "info")
            real_id = self.install_profile(p, progress)
            p.version_id = real_id
            self._persist_version(p)

            cmd = self.build_command(p, acc)
            self.log("Comando: " + " ".join(shlex.quote(c) for c in cmd[:6]) + " ...", "muted")
            flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            on_state("running")
            self.process = subprocess.Popen(
                cmd, cwd=str(MC_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
            for line in self.process.stdout:            # ← logs en tiempo real
                self.log(line.rstrip(), self._level(line))
            code = self.process.wait()
            self.log(f"== Minecraft cerró con código {code} ==", "info" if code == 0 else "error")
        except FileNotFoundError:
            self.log("No se encontró Java. Instalá JDK 17/21 o indicá la ruta en Configuración.", "error")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Error: {exc}", "error")
        finally:
            on_state("idle")

    def stop(self):
        if self.running:
            self.process.terminate()
            self.log("Proceso terminado por el usuario.", "warn")

    def _persist_version(self, p: Profile):
        profiles = self.config.cfg.profiles
        i = self.config.cfg.active_profile
        if 0 <= i < len(profiles):
            profiles[i]["version_id"] = p.version_id
            self.config.save()

    @staticmethod
    def _level(line: str) -> str:
        up = line.upper()
        if "ERROR" in up or "EXCEPTION" in up or "FATAL" in up:
            return "error"
        if "WARN" in up:
            return "warn"
        return "log"


# ───────────────────────────── 7. CONSOLA ─────────────────────────────
class ConsolePanel(ctk.CTkFrame):
    """Panel retráctil con salida del juego. log() es thread-safe (usa una cola)."""
    COLORS = {"log": Theme.TEXT, "info": Theme.BLUE2, "warn": "#f5c76a",
              "error": Theme.DANGER, "muted": Theme.MUTED}

    def __init__(self, master):
        super().__init__(master, fg_color=Theme.PANEL, border_color=Theme.BORDER, border_width=1, corner_radius=14)
        self.q: queue.Queue = queue.Queue()
        self.expanded = False

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(8, 6))
        ctk.CTkLabel(bar, text="CONSOLA", font=font(11, "bold"), text_color=Theme.BLUE2).pack(side="left")
        self.toggle_btn = ctk.CTkButton(bar, text="▲ Expandir", width=90, height=26, font=font(12),
                                        fg_color=Theme.PANEL2, hover_color="#172438", command=self.toggle)
        self.toggle_btn.pack(side="right")
        ctk.CTkButton(bar, text="Limpiar", width=70, height=26, font=font(12), fg_color=Theme.PANEL2,
                      hover_color="#172438", command=self.clear).pack(side="right", padx=6)

        self.box = ctk.CTkTextbox(self, height=0, fg_color=Theme.INPUT, border_color=Theme.BORDER, border_width=1,
                                  font=ctk.CTkFont(family="Consolas", size=12), corner_radius=10)
        for lvl, color in self.COLORS.items():
            self.box._textbox.tag_config(lvl, foreground=color)  # tags de color por nivel
        self.box.configure(state="disabled")
        self.after(100, self._drain)

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.box.configure(height=220)
            self.box.pack(fill="both", expand=True, padx=14, pady=(0, 12))
            self.toggle_btn.configure(text="▼ Ocultar")
        else:
            self.box.pack_forget()
            self.toggle_btn.configure(text="▲ Expandir")

    def show(self):
        if not self.expanded:
            self.toggle()

    def log(self, text: str, level: str = "log"):
        self.q.put((text, level))  # seguro desde cualquier hilo

    def clear(self):
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.configure(state="disabled")

    def _drain(self):
        """Vacía la cola en el hilo de UI (cada 100 ms) y hace autoscroll."""
        try:
            self.box.configure(state="normal")
            for _ in range(300):  # tope por ciclo para no congelar la UI
                text, level = self.q.get_nowait()
                stamp = time.strftime("%H:%M:%S")
                self.box._textbox.insert("end", f"[{stamp}] {text}\n", level)
        except queue.Empty:
            pass
        finally:
            self.box.configure(state="disabled")
            self.box.see("end")
            self.after(100, self._drain)


# ───────────────────────────── 6. UI: widgets base ─────────────────────────────
class Card(ctk.CTkFrame):
    def __init__(self, master, **kw):
        super().__init__(master, fg_color=Theme.PANEL, border_color=Theme.BORDER, border_width=1,
                         corner_radius=16, **kw)


def primary_btn(master, text, command, **kw):
    return ctk.CTkButton(master, text=text, command=command, fg_color=Theme.BLUE, hover_color=Theme.BLUE_HOVER,
                         font=font(13, "bold"), corner_radius=10, height=36, **kw)


def ghost_btn(master, text, command, **kw):
    return ctk.CTkButton(master, text=text, command=command, fg_color=Theme.PANEL2, hover_color="#172438",
                         border_color=Theme.BORDER, border_width=1, text_color=Theme.TEXT,
                         font=font(13, "bold"), corner_radius=10, height=36, **kw)


def page_header(master, eyebrow, title, subtitle=""):
    f = ctk.CTkFrame(master, fg_color="transparent")
    ctk.CTkLabel(f, text=eyebrow.upper(), font=font(11, "bold"), text_color=Theme.BLUE2).pack(anchor="w")
    ctk.CTkLabel(f, text=title, font=font(28, "bold"), text_color=Theme.TEXT).pack(anchor="w", pady=(4, 2))
    if subtitle:
        ctk.CTkLabel(f, text=subtitle, font=font(13), text_color=Theme.MUTED, justify="left").pack(anchor="w")
    return f


class Page(ctk.CTkScrollableFrame):
    def __init__(self, app: "FexCraftApp"):
        super().__init__(app.content, fg_color="transparent", scrollbar_button_color=Theme.BORDER)
        self.app = app

    def on_show(self):  # se llama cada vez que la pestaña se muestra
        pass


# ---------------------------- Inicio / Noticias ----------------------------
class HomePage(Page):
    def __init__(self, app):
        super().__init__(app)
        page_header(self, "Launcher", "Todo listo para jugar.",
                    "Elegí cuenta y perfil, y presioná JUGAR.").pack(fill="x", pady=(0, 18))

        # Tarjeta de lanzamiento
        card = Card(self)
        card.pack(fill="x", pady=(0, 16))
        left = ctk.CTkFrame(card, fg_color="transparent")
        left.pack(side="left", padx=22, pady=20, fill="x", expand=True)
        self.lbl_account = ctk.CTkLabel(left, text="", font=font(14, "bold"), anchor="w")
        self.lbl_account.pack(anchor="w")
        self.lbl_profile = ctk.CTkLabel(left, text="", font=font(12), text_color=Theme.MUTED, anchor="w")
        self.lbl_profile.pack(anchor="w", pady=(4, 10))
        self.progress = ctk.CTkProgressBar(left, progress_color=Theme.BLUE, fg_color="#253247", height=8)
        self.progress.set(0)
        self.progress.pack(fill="x")
        self.lbl_status = ctk.CTkLabel(left, text="Inactivo", font=font(11), text_color=Theme.MUTED)
        self.lbl_status.pack(anchor="w", pady=(6, 0))

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(side="right", padx=22)
        self.play_btn = ctk.CTkButton(btns, text="▶  JUGAR", width=170, height=52, font=font(16, "bold"),
                                      fg_color=Theme.BLUE, hover_color=Theme.BLUE_HOVER, corner_radius=12,
                                      command=app.play_or_stop)
        self.play_btn.pack()

        ctk.CTkLabel(self, text="Novedades", font=font(18, "bold")).pack(anchor="w", pady=(6, 10))
        self.news_box = ctk.CTkFrame(self, fg_color="transparent")
        self.news_box.pack(fill="x")
        self._news_loaded = False

    def on_show(self):
        acc, prof = self.app.config.active_account(), self.app.config.active_profile()
        self.lbl_account.configure(text=f"👤  {acc.username} ({acc.type})" if acc else "👤  Sin cuenta — agregá una en Configuración")
        self.lbl_profile.configure(text=f"Perfil: {prof.name} · {prof.kind} {prof.mc_version}" if prof
                                   else "Sin perfil — creá uno en Instalaciones")
        if not self._news_loaded:
            self._news_loaded = True
            self.app.run_bg(self.app.versions.news, self._render_news)

    def _render_news(self, items):
        for it in items:
            c = Card(self.news_box)
            c.pack(fill="x", pady=5)
            ctk.CTkLabel(c, text=it["tag"].upper(), font=font(10, "bold"), text_color=Theme.BLUE2).pack(anchor="w", padx=18, pady=(14, 0))
            ctk.CTkLabel(c, text=it["title"], font=font(15, "bold")).pack(anchor="w", padx=18)
            ctk.CTkLabel(c, text=it["text"], font=font(12), text_color=Theme.MUTED).pack(anchor="w", padx=18, pady=(2, 14))

    def set_state(self, state: str):
        txt = {"idle": ("▶  JUGAR", Theme.BLUE, "Inactivo"),
               "installing": ("⏳  Instalando…", Theme.PANEL2, "Descargando archivos…"),
               "running": ("■  DETENER", Theme.DANGER, "Minecraft en ejecución")}[state]
        self.play_btn.configure(text=txt[0], fg_color=txt[1])
        self.lbl_status.configure(text=txt[2])
        if state != "installing":
            self.progress.set(0 if state == "idle" else 1)


# ---------------------------- Instalaciones ----------------------------
class InstallationsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.all_versions: list[dict] = []
        page_header(self, "Instalaciones", "Versiones y perfiles",
                    "Creá perfiles con versiones oficiales, snapshots, Fabric o Forge.").pack(fill="x", pady=(0, 18))

        card = Card(self)
        card.pack(fill="x", pady=(0, 16))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=18)

        ctk.CTkLabel(inner, text="Tipo", font=font(12, "bold")).grid(row=0, column=0, sticky="w")
        self.kind = ctk.CTkSegmentedButton(inner, values=["Release", "Snapshot", "Fabric", "Forge"],
                                           selected_color=Theme.BLUE, selected_hover_color=Theme.BLUE_HOVER,
                                           unselected_color=Theme.PANEL2, fg_color=Theme.INPUT,
                                           command=lambda _v: self.refresh_versions())
        self.kind.set("Release")
        self.kind.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 14))

        ctk.CTkLabel(inner, text="Versión de Minecraft", font=font(12, "bold")).grid(row=2, column=0, sticky="w")
        ctk.CTkLabel(inner, text="Nombre del perfil", font=font(12, "bold")).grid(row=2, column=1, sticky="w", padx=(12, 0))
        self.version_menu = ctk.CTkComboBox(inner, values=["Cargando…"], fg_color=Theme.INPUT, border_color=Theme.BORDER,
                                            button_color=Theme.PANEL2, dropdown_fg_color=Theme.PANEL, height=36)
        self.version_menu.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.name_entry = ctk.CTkEntry(inner, placeholder_text="Mi perfil", fg_color=Theme.INPUT,
                                       border_color=Theme.BORDER, height=36)
        self.name_entry.grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=(6, 0))
        inner.columnconfigure((0, 1), weight=1)

        self.msg = ctk.CTkLabel(inner, text="", font=font(12), text_color=Theme.MUTED)
        self.msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        primary_btn(inner, "＋ Crear perfil", self.create_profile).grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))

        ctk.CTkLabel(self, text="Mis perfiles", font=font(18, "bold")).pack(anchor="w", pady=(6, 10))
        self.list_box = ctk.CTkFrame(self, fg_color="transparent")
        self.list_box.pack(fill="x")

    def on_show(self):
        if not self.all_versions:
            self.app.run_bg(self.app.versions.minecraft_versions, self._got_versions, self._err)
        self.render_profiles()

    def _err(self, exc):
        self.msg.configure(text=f"No se pudieron cargar versiones: {exc}", text_color=Theme.DANGER)

    def _got_versions(self, versions):
        self.all_versions = versions
        self.refresh_versions()

    def refresh_versions(self):
        kind = self.kind.get()
        if kind == "Snapshot":
            ids = [v["id"] for v in self.all_versions if v["type"] == "snapshot"][:60]
        else:  # Release / Fabric / Forge parten de las versiones estables
            ids = [v["id"] for v in self.all_versions if v["type"] == "release"]
        if ids:
            self.version_menu.configure(values=ids)
            self.version_menu.set(ids[0])

    def create_profile(self):
        mc = self.version_menu.get().strip()
        kind = self.kind.get().lower()
        if not mc or mc == "Cargando…":
            return
        name = self.name_entry.get().strip() or f"{self.kind.get()} {mc}"
        # version_id definitivo se resuelve al instalar (loader + versión)
        prof = Profile(name=name, version_id=mc, mc_version=mc, kind=kind)
        cfg = self.app.config.cfg
        cfg.profiles.append(asdict(prof))
        cfg.active_profile = len(cfg.profiles) - 1
        self.app.config.save()
        self.name_entry.delete(0, "end")
        self.msg.configure(text=f"Perfil «{name}» creado. Se instalará al presionar JUGAR.", text_color=Theme.GREEN)
        self.render_profiles()

    def render_profiles(self):
        for w in self.list_box.winfo_children():
            w.destroy()
        cfg = self.app.config.cfg
        if not cfg.profiles:
            ctk.CTkLabel(self.list_box, text="Todavía no hay perfiles.", text_color=Theme.MUTED).pack(anchor="w")
        for i, p in enumerate(self.app.config.profiles):
            row = Card(self.list_box)
            row.pack(fill="x", pady=4)
            active = i == cfg.active_profile
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", padx=18, pady=14)
            ctk.CTkLabel(info, text=("● " if active else "") + p.name, font=font(14, "bold"),
                         text_color=Theme.BLUE2 if active else Theme.TEXT).pack(anchor="w")
            ctk.CTkLabel(info, text=f"{p.kind} · Minecraft {p.mc_version}", font=font(11), text_color=Theme.MUTED).pack(anchor="w")
            ctk.CTkButton(row, text="×", width=34, height=34, fg_color="transparent", hover_color=Theme.PANEL2,
                          text_color=Theme.MUTED, command=lambda i=i: self.delete(i)).pack(side="right", padx=(0, 12))
            ghost_btn(row, "Seleccionada" if active else "Seleccionar", lambda i=i: self.select(i),
                      width=120).pack(side="right", padx=6)

    def select(self, i):
        self.app.config.cfg.active_profile = i
        self.app.config.save()
        self.render_profiles()

    def delete(self, i):
        cfg = self.app.config.cfg
        cfg.profiles.pop(i)
        cfg.active_profile = min(cfg.active_profile, len(cfg.profiles) - 1)
        self.app.config.save()
        self.render_profiles()


# ---------------------------- Gestor de Mods ----------------------------
class ModsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        page_header(self, "Mods", "Gestor de mods",
                    "Activá o desactivá mods (.jar) de Fabric/Forge. Los desactivados se renombran a .jar.disabled.").pack(fill="x", pady=(0, 14))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 12))
        primary_btn(bar, "＋ Añadir mod (.jar)", self.add_mod).pack(side="left")
        ghost_btn(bar, "Abrir carpeta", lambda: open_folder(MODS_DIR)).pack(side="left", padx=8)
        ghost_btn(bar, "↻ Actualizar", self.render).pack(side="left")
        self.list_box = ctk.CTkFrame(self, fg_color="transparent")
        self.list_box.pack(fill="x")

    def on_show(self):
        self.render()

    def add_mod(self):
        files = filedialog.askopenfilenames(title="Elegí mods", filetypes=[("Mods", "*.jar")])
        for f in files:
            shutil.copy2(f, MODS_DIR / Path(f).name)
            self.app.console.log(f"Mod añadido: {Path(f).name}", "info")
        self.render()

    def toggle(self, path: Path, enabled: bool):
        """Activar = quitar .disabled; desactivar = agregarlo."""
        new = path.with_suffix("") if path.suffix == ".disabled" else path.with_name(path.name + ".disabled")
        path.rename(new)
        self.render()

    def delete(self, path: Path):
        if messagebox.askyesno("Eliminar mod", f"¿Eliminar {path.name}?"):
            path.unlink(missing_ok=True)
            self.render()

    def render(self):
        for w in self.list_box.winfo_children():
            w.destroy()
        mods = sorted(list(MODS_DIR.glob("*.jar")) + list(MODS_DIR.glob("*.jar.disabled")), key=lambda p: p.name.lower())
        if not mods:
            ctk.CTkLabel(self.list_box, text="No hay mods. Agregá archivos .jar compatibles con tu loader.",
                         text_color=Theme.MUTED).pack(anchor="w")
        for m in mods:
            enabled = m.suffix == ".jar"
            row = Card(self.list_box)
            row.pack(fill="x", pady=4)
            name = m.name.replace(".disabled", "")
            ctk.CTkLabel(row, text=name, font=font(13, "bold"),
                         text_color=Theme.TEXT if enabled else Theme.MUTED).pack(side="left", padx=18, pady=14)
            ctk.CTkButton(row, text="×", width=34, height=34, fg_color="transparent", hover_color=Theme.PANEL2,
                          text_color=Theme.MUTED, command=lambda m=m: self.delete(m)).pack(side="right", padx=(0, 12))
            sw = ctk.CTkSwitch(row, text="", progress_color=Theme.BLUE, width=44,
                               command=lambda m=m, e=enabled: self.toggle(m, e))
            sw.pack(side="right", padx=8)
            sw.select() if enabled else sw.deselect()


# ---------------------------- Configuración ----------------------------
class SettingsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        s = app.config.settings
        page_header(self, "Configuración", "Cuenta y rendimiento").pack(fill="x", pady=(0, 18))

        # --- Cuentas
        acc = Card(self)
        acc.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(acc, text="Cuentas", font=font(16, "bold")).pack(anchor="w", padx=20, pady=(16, 8))
        row = ctk.CTkFrame(acc, fg_color="transparent")
        row.pack(fill="x", padx=20)
        self.offline_entry = ctk.CTkEntry(row, placeholder_text="Nombre de usuario (offline)", height=36,
                                          fg_color=Theme.INPUT, border_color=Theme.BORDER)
        self.offline_entry.pack(side="left", fill="x", expand=True)
        primary_btn(row, "Añadir offline", self.add_offline).pack(side="left", padx=8)
        self.acc_list = ctk.CTkFrame(acc, fg_color="transparent")
        self.acc_list.pack(fill="x", padx=20, pady=(10, 16))

        # --- Rendimiento
        perf = Card(self)
        perf.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(perf, text="Rendimiento", font=font(16, "bold")).pack(anchor="w", padx=20, pady=(16, 8))
        max_ram = 16384
        if psutil:  # no ofrecer más del 75% de la RAM física
            max_ram = max(2048, int(psutil.virtual_memory().total / 1048576 * 0.75) // 512 * 512)
        self.max_ram = max_ram
        self.ram_label = ctk.CTkLabel(perf, text="", font=font(13, "bold"), text_color=Theme.BLUE2)
        self.ram_label.pack(anchor="w", padx=20)
        self.ram = ctk.CTkSlider(perf, from_=1024, to=max_ram, number_of_steps=(max_ram - 1024) // 512,
                                 progress_color=Theme.BLUE, button_color=Theme.BLUE2, button_hover_color=Theme.BLUE,
                                 command=self._ram_changed)
        self.ram.set(min(s.ram_mb, max_ram))
        self.ram.pack(fill="x", padx=20, pady=(6, 12))
        self._ram_changed(self.ram.get())

        ctk.CTkLabel(perf, text="Argumentos de JVM", font=font(12, "bold")).pack(anchor="w", padx=20)
        self.jvm = ctk.CTkEntry(perf, height=36, fg_color=Theme.INPUT, border_color=Theme.BORDER)
        self.jvm.insert(0, s.jvm_args)
        self.jvm.pack(fill="x", padx=20, pady=(6, 12))

        res = ctk.CTkFrame(perf, fg_color="transparent")
        res.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkLabel(res, text="Resolución", font=font(12, "bold")).pack(side="left")
        self.res_w = ctk.CTkEntry(res, width=80, height=34, fg_color=Theme.INPUT, border_color=Theme.BORDER)
        self.res_h = ctk.CTkEntry(res, width=80, height=34, fg_color=Theme.INPUT, border_color=Theme.BORDER)
        self.res_w.insert(0, str(s.res_w)); self.res_h.insert(0, str(s.res_h))
        self.res_w.pack(side="left", padx=(12, 6)); ctk.CTkLabel(res, text="×").pack(side="left"); self.res_h.pack(side="left", padx=6)

        ctk.CTkLabel(perf, text="Ruta de Java (vacío = usar PATH)", font=font(12, "bold")).pack(anchor="w", padx=20, pady=(10, 0))
        jrow = ctk.CTkFrame(perf, fg_color="transparent")
        jrow.pack(fill="x", padx=20, pady=(6, 16))
        self.java = ctk.CTkEntry(jrow, height=36, fg_color=Theme.INPUT, border_color=Theme.BORDER)
        self.java.insert(0, s.java_path)
        self.java.pack(side="left", fill="x", expand=True)
        ghost_btn(jrow, "Buscar", self.pick_java, width=80).pack(side="left", padx=8)

        primary_btn(self, "Guardar configuración", self.save).pack(anchor="e")
        self.status = ctk.CTkLabel(self, text="", font=font(12))
        self.status.pack(anchor="e", pady=6)

    def on_show(self):
        self.render_accounts()

    def _ram_changed(self, v):
        mb = int(round(v / 512) * 512)
        self.ram_label.configure(text=f"Memoria RAM: {mb / 1024:.1f} GB  (máx. {self.max_ram / 1024:.0f} GB)")

    def pick_java(self):
        p = filedialog.askopenfilename(title="Seleccioná java / javaw")
        if p:
            self.java.delete(0, "end"); self.java.insert(0, p)

    def save(self):
        try:
            s = Settings(ram_mb=int(round(self.ram.get() / 512) * 512), jvm_args=self.jvm.get().strip(),
                         java_path=self.java.get().strip(), res_w=int(self.res_w.get()), res_h=int(self.res_h.get()))
            shlex.split(s.jvm_args)  # valida comillas
        except ValueError as e:
            self.status.configure(text=f"Valor inválido: {e}", text_color=Theme.DANGER)
            return
        self.app.config.set_settings(s)
        self.status.configure(text="✓ Configuración guardada", text_color=Theme.GREEN)

    # --- cuentas
    def add_offline(self):
        try:
            self.app.accounts.add_offline(self.offline_entry.get())
            self.offline_entry.delete(0, "end")
            self.render_accounts()
        except ValueError as e:
            self.status.configure(text=str(e), text_color=Theme.DANGER)

    def render_accounts(self):
        for w in self.acc_list.winfo_children():
            w.destroy()
        cfg = self.app.config.cfg
        for i, a in enumerate(self.app.config.accounts):
            r = ctk.CTkFrame(self.acc_list, fg_color=Theme.PANEL2, corner_radius=10)
            r.pack(fill="x", pady=3)
            active = i == cfg.active_account
            ctk.CTkLabel(r, text=f"{'● ' if active else ''}{a.username}  ·  {a.type}", font=font(13, "bold"),
                         text_color=Theme.BLUE2 if active else Theme.TEXT).pack(side="left", padx=14, pady=10)
            ctk.CTkButton(r, text="×", width=30, fg_color="transparent", hover_color=Theme.BORDER, text_color=Theme.MUTED,
                          command=lambda i=i: (self.app.accounts.remove(i), self.render_accounts())).pack(side="right", padx=6)
            ctk.CTkButton(r, text="Usar", width=60, height=28, fg_color=Theme.PANEL, hover_color=Theme.BORDER,
                          command=lambda i=i: (setattr(cfg, "active_account", i), self.app.config.save(), self.render_accounts())
                          ).pack(side="right")


# ───────────────────────────── 8. APP PRINCIPAL ─────────────────────────────
class FexCraftApp(ctk.CTk):
    NAV = [("home", "⌂  Inicio", HomePage), ("installs", "▤  Instalaciones", InstallationsPage),
           ("mods", "✦  Mods", ModsPage), ("settings", "⚙  Configuración", SettingsPage)]

    def __init__(self):
        super().__init__(fg_color=Theme.BG)
        ctk.set_appearance_mode("dark")
        self.title("FexCraft")
        self.geometry("1100x760")
        self.minsize(900, 620)

        self.config = ConfigManager()
        self.accounts = AccountManager(self.config)
        self.versions = VersionService()
        self._ui_queue: queue.Queue = queue.Queue()
        self.console = ConsolePanel(self)  # creado antes: el launcher lo usa para loguear
        self.launcher = GameLauncher(self.config, self.console.log)

        # Layout: sidebar | (contenido + consola)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=(18, 0))
        self.console.grid(row=1, column=1, sticky="ew", padx=(0, 18), pady=14)

        self.pages = {key: cls(self) for key, _label, cls in self.NAV}
        self.current: Optional[str] = None
        self.show_page("home")
        self.after(50, self._pump_ui)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Ícono de la ventana (Windows). CustomTkinter pisa el ícono a los ~200 ms,
        # por eso se aplica con retraso. Si no existe el .ico, se ignora sin error.
        if sys.platform.startswith("win"):
            ico = resource_path("fexcraft.ico")
            if os.path.exists(ico):
                self.after(250, lambda: self.iconbitmap(ico))

    # ---- sidebar
    def _build_sidebar(self):
        side = ctk.CTkFrame(self, fg_color=Theme.PANEL, corner_radius=0, width=220, border_width=0)
        side.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(0, 18))
        side.grid_propagate(False)
        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=20, pady=(26, 28))
        ctk.CTkLabel(brand, text="F", width=44, height=44, corner_radius=13, fg_color=Theme.BLUE,
                     font=font(22, "bold")).pack(side="left")
        txt = ctk.CTkFrame(brand, fg_color="transparent")
        txt.pack(side="left", padx=12)
        ctk.CTkLabel(txt, text="FexCraft", font=font(18, "bold")).pack(anchor="w")
        ctk.CTkLabel(txt, text="Launcher", font=font(11), text_color=Theme.MUTED).pack(anchor="w")

        self.nav_btns = {}
        for key, label, _cls in self.NAV:
            b = ctk.CTkButton(side, text=label, anchor="w", height=42, corner_radius=10, font=font(13, "bold"),
                              fg_color="transparent", hover_color=Theme.PANEL2, text_color=Theme.MUTED,
                              command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=14, pady=3)
            self.nav_btns[key] = b
        ctk.CTkLabel(side, text="v1.0 · hecho con ♥", font=font(10), text_color=Theme.MUTED).pack(side="bottom", pady=16)

    def show_page(self, key: str):
        if self.current:
            self.pages[self.current].pack_forget()
            self.nav_btns[self.current].configure(fg_color="transparent", text_color=Theme.MUTED)
        self.current = key
        self.nav_btns[key].configure(fg_color="#112e4d", text_color=Theme.BLUE2)
        self.pages[key].pack(fill="both", expand=True)
        self.pages[key].on_show()

    # ---- hilos ↔ UI
    def ui(self, fn: Callable, *args):
        """Encola una función para ejecutarla en el hilo de Tk (seguro desde hilos)."""
        self._ui_queue.put((fn, args))

    def _pump_ui(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(50, self._pump_ui)

    def run_bg(self, work: Callable, on_ok: Optional[Callable] = None, on_err: Optional[Callable] = None):
        """Ejecuta work() en un hilo y entrega el resultado a on_ok/on_err en el hilo de UI."""
        def target():
            try:
                res = work()
                if on_ok:
                    self.ui(on_ok, res)
            except Exception as exc:  # noqa: BLE001
                self.console.log(f"Error: {exc}", "error")
                if on_err:
                    self.ui(on_err, exc)
        threading.Thread(target=target, daemon=True).start()

    # ---- botón JUGAR
    def play_or_stop(self):
        if self.launcher.running:
            self.launcher.stop()
            return
        acc, prof = self.config.active_account(), self.config.active_profile()
        if not acc:
            messagebox.showinfo("FexCraft", "Agregá una cuenta en Configuración.")
            return self.show_page("settings")
        if not prof:
            messagebox.showinfo("FexCraft", "Creá un perfil en Instalaciones.")
            return self.show_page("installs")
        if mll is None:
            messagebox.showerror("FexCraft", "Falta la librería: pip install minecraft-launcher-lib")
            return

        home: HomePage = self.pages["home"]  # type: ignore[assignment]
        self.console.show()
        # Los callbacks del hilo se reenvían al hilo de UI con self.ui(...)
        progress = lambda frac, txt: self.ui(home.progress.set, min(max(frac, 0), 1))
        on_state = lambda st: self.ui(home.set_state, st)
        threading.Thread(target=self.launcher.play, args=(prof, acc, progress, on_state), daemon=True).start()

    def _on_close(self):
        if self.launcher.running and not messagebox.askyesno("FexCraft", "Minecraft sigue abierto. ¿Cerrar igual?"):
            return
        self.destroy()


if __name__ == "__main__":
    FexCraftApp().mainloop()