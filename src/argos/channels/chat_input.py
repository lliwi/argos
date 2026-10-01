"""Entrada del chat (`argos chat`): edición de línea, historial, pegado y adjuntos.

- Flechas ←/→ para moverse por el texto, ↑/↓ por las líneas y el historial (guardado en disco),
  atajos de Emacs habituales (Ctrl-A/E/W/K…).
- Enter envía; Mayús-Enter, Alt-Enter o Ctrl-J insertan un salto de línea. Pegar texto
  multilínea (pegado entre corchetes del terminal) lo inserta entero sin enviarlo.
- Adjuntos: `/adjuntar <ruta…>` (con autocompletado de rutas), arrastrar ficheros al terminal
  (pega sus rutas y se detectan) o Ctrl-V con una imagen en el portapapeles (Wayland/X11).
  Se envían con el siguiente mensaje; `/adjuntos` los lista y `/quitar` los descarta.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse

from argos.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES,
    Attachment,
    AttachmentError,
    check,
    dedupe,
    safe_name,
)

COMMANDS = [
    "/adjuntar",
    "/adjuntos",
    "/quitar",
    "/new",
    "/threads",
    "/memoria",
    "/herramientas",
    "/help",
]
HINT = (
    "Enter enviar · Mayús-Enter salto de línea · Ctrl-V pegar imagen · /adjuntar ruta · "
    "Ctrl-D salir"
)
# Mayús/Alt+Enter llegan como secuencias propias solo si el terminal las distingue: xterm
# modifyOtherKeys (`ESC[27;<mod>;13~`, lo que usa Herdr) o protocolo kitty (`ESC[13;<mod>u`).
# prompt_toolkit trata algunas como Enter normal (enviaría); aquí son salto de línea.
NEWLINE_SEQUENCES = (
    "\x1b[27;2;13~",
    "\x1b[13;2u",  # Mayús+Enter
    "\x1b[27;3;13~",
    "\x1b[13;3u",  # Alt+Enter
    "\x1b[27;4;13~",
    "\x1b[13;4u",
)  # Mayús+Alt+Enter
MODIFY_OTHER_KEYS_ON = "\x1b[>4;1m"
MODIFY_OTHER_KEYS_OFF = "\x1b[>4;0m"


def _register_newline_sequences() -> None:
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    for seq in NEWLINE_SEQUENCES:
        ANSI_SEQUENCES[seq] = Keys.ControlJ  # ligado a «insertar salto de línea»


# --------------------------------------------------------------------------- adjuntos


def load_file(path: str | Path) -> Attachment:
    p = Path(path).expanduser()
    if not p.is_file():
        raise AttachmentError(f"no existe o no es un fichero: {p}")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise AttachmentError(f"'{p.name}' pesa {size // 2**20} MB (máx {MAX_FILE_BYTES // 2**20})")
    return Attachment(safe_name(p.name), p.read_bytes())


def paths_from_paste(text: str) -> list[Path] | None:
    """Si lo pegado son solo rutas de ficheros existentes (arrastrar y soltar), las devuelve.
    Admite comillas, espacios escapados y URIs file://. Cualquier otra cosa => None (es texto)."""
    text = text.strip()
    if not text or len(text) > 4096:
        return None
    try:
        tokens = [t for line in text.splitlines() for t in shlex.split(line)]
    except ValueError:
        return None
    paths: list[Path] = []
    for tok in tokens:
        if tok.startswith("file://"):
            tok = unquote(urlparse(tok).path)
        if not tok.startswith(("/", "~")):
            return None
        p = Path(tok).expanduser()
        if not p.is_file():
            return None
        paths.append(p)
    return paths or None


def _run(cmd: list[str], timeout: float = 3) -> bytes | None:
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def clipboard_image() -> tuple[bytes, str] | None:
    """(bytes, extensión) de la imagen del portapapeles, o None."""
    if shutil.which("wl-paste"):
        types = (_run(["wl-paste", "--list-types"]) or b"").decode(errors="replace").split()
        images = [t for t in types if t.startswith("image/")]
        if images:
            mime = "image/png" if "image/png" in images else images[0]
            data = _run(["wl-paste", "--no-newline", "--type", mime], timeout=5)
            if data:
                return data, "." + mime.split("/", 1)[1].split("+")[0].replace("jpeg", "jpg")
        return None
    if shutil.which("xclip"):
        targets = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]) or b""
        if b"image/png" in targets:
            data = _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], timeout=5)
            if data:
                return data, ".png"
    return None


def clipboard_text() -> str | None:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            data = _run(cmd)
            return data.decode(errors="replace") if data is not None else None
    return None


class Pending:
    """Adjuntos que se enviarán con el próximo mensaje."""

    def __init__(self) -> None:
        self.items: list[Attachment] = []

    def add(self, att: Attachment) -> Attachment:
        if len(self.items) >= MAX_FILES:
            raise AttachmentError(f"máximo {MAX_FILES} adjuntos por mensaje")
        att = Attachment(dedupe(att.name, {a.name for a in self.items}), att.data)
        check([*self.items, att])
        self.items.append(att)
        return att

    def add_paths(self, paths: Iterable[str | Path]) -> list[str]:
        return [self.add(load_file(p)).name for p in paths]

    def remove(self, which: str = "") -> list[str]:
        if not which or which in ("todo", "todos", "all"):
            gone, self.items = [a.name for a in self.items], []
            return gone
        if which.isdigit() and 1 <= int(which) <= len(self.items):
            return [self.items.pop(int(which) - 1).name]
        keep = [a for a in self.items if a.name != which]
        gone = [a.name for a in self.items if a.name == which]
        self.items = keep
        return gone

    def take(self) -> list[Attachment]:
        items, self.items = self.items, []
        return items

    def summary(self) -> str:
        return ", ".join(f"{a.name}{' 🖼' if a.is_image else ''}" for a in self.items)


# --------------------------------------------------------------------------- prompt


class ChatInput:
    """Prompt interactivo del chat. `read()` devuelve el texto (o lanza EOFError con Ctrl-D)."""

    def __init__(
        self,
        history_path: Path,
        pending: Pending,
        image_source: Callable[[], tuple[bytes, str] | None] = clipboard_image,
        text_source: Callable[[], str | None] = clipboard_text,
    ) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        self.pending = pending
        self.flash = ""
        self._flash_until = 0.0
        self._image_source = image_source
        self._text_source = text_source
        _register_newline_sequences()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self.session: PromptSession[str] = PromptSession(
            multiline=True,
            key_bindings=self._bindings(),
            history=FileHistory(str(history_path)),
            completer=_completer_class()(),
            complete_while_typing=False,
            bottom_toolbar=self._toolbar,
            prompt_continuation="     … ",
        )

    def say(self, msg: str) -> None:
        self.flash, self._flash_until = msg, time.monotonic() + 6

    def _toolbar(self) -> str:
        parts = []
        if self.flash and time.monotonic() < self._flash_until:
            parts.append(self.flash)
        if self.pending.items:
            parts.append(
                f"📎 {len(self.pending.items)}: {self.pending.summary()} (/quitar para descartar)"
            )
        return " · ".join(parts) or HINT

    def paste(self, data: str) -> str | None:
        """Gestiona un pegado: rutas de fichero => adjuntos; si no, devuelve el texto a insertar."""
        paths = paths_from_paste(data)
        if paths:
            try:
                names = self.pending.add_paths(paths)
                self.say(f"adjuntado: {', '.join(names)}")
            except AttachmentError as exc:
                self.say(f"⚠ {exc}")
            return None
        return data.replace("\r\n", "\n").replace("\r", "\n")

    def paste_clipboard(self) -> str | None:
        """Ctrl-V: imagen del portapapeles => adjunto; si hay texto, lo devuelve para insertarlo."""
        image = self._image_source()
        if image:
            data, ext = image
            try:
                att = self.pending.add(
                    Attachment(f"portapapeles-{time.strftime('%H%M%S')}{ext}", data)
                )
                self.say(f"imagen adjuntada: {att.name}")
            except AttachmentError as exc:
                self.say(f"⚠ {exc}")
            return None
        text = self._text_source()
        if text is None:
            self.say("⚠ portapapeles no disponible (instala wl-clipboard o xclip)")
            return None
        return self.paste(text)

    def _bindings(self):
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        kb = KeyBindings()

        @kb.add("enter")
        def _(event) -> None:
            buf = event.current_buffer
            if buf.complete_state:  # Enter en el menú de autocompletado: elegir
                buf.complete_state = None
                return
            buf.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.BracketedPaste)
        def _(event) -> None:
            text = self.paste(event.data)
            if text:
                event.current_buffer.insert_text(text)

        @kb.add("c-v")
        def _(event) -> None:
            text = self.paste_clipboard()
            if text:
                event.current_buffer.insert_text(text)
            event.app.invalidate()

        return kb

    def read(self, prompt: str = "argos> ") -> str:
        """Lee un mensaje. Mientras se escribe pide al terminal modifyOtherKeys (nivel 1) para que
        Mayús+Enter sea distinguible de Enter; al salir lo restablece."""
        import sys

        tty = sys.stdout.isatty()
        if tty:
            sys.stdout.write(MODIFY_OTHER_KEYS_ON)
            sys.stdout.flush()
        try:
            return self.session.prompt(prompt)
        finally:
            if tty:
                sys.stdout.write(MODIFY_OTHER_KEYS_OFF)
                sys.stdout.flush()


def _completer_class():
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document

    class Impl(Completer):
        def __init__(self) -> None:
            self._paths = PathCompleter(expanduser=True)

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/adjuntar "):
                arg = text[len("/adjuntar ") :].split(" ")[-1]
                yield from self._paths.get_completions(Document(arg, len(arg)), complete_event)
            elif text.startswith("/") and " " not in text:
                for cmd in COMMANDS:
                    if cmd.startswith(text):
                        yield Completion(cmd, start_position=-len(text))

    return Impl
