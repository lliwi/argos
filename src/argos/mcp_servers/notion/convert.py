"""Conversiones entre Notion y formas simples para el modelo.

- Bloques → texto tipo Markdown (lectura).
- Markdown sencillo → bloques (escritura): títulos, listas, tareas, citas, código, separadores y
  párrafos; en línea **negrita**, `código` y [enlaces](https://…).
- Propiedades: valores simples ({"Estado": "En progreso", "Área": ["NAS"]}) ↔ formato Notion,
  guiado por el esquema real de la base de datos (nunca se inventan tipos).
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT = 2000   # límite de Notion por objeto rich_text
_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)")
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")


def normalize_id(raw: str) -> str | None:
    """Acepta un id de Notion con o sin guiones, o una URL de Notion que acabe en el id."""
    raw = (raw or "").strip().lower().split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"([0-9a-f]{32}|[0-9a-f-]{36})$", raw)
    if not m or not _UUID.match(m.group(1)):
        return None
    h = m.group(1).replace("-", "")
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# --------------------------------------------------------------------------- lectura

def plain(rich: list[dict] | None) -> str:
    return "".join(r.get("plain_text", "") for r in rich or [])


def title_of(obj: dict) -> str:
    if obj.get("object") == "database":
        return plain(obj.get("title"))
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title":
            return plain(prop.get("title"))
    return ""


def block_text(block: dict, number: int = 1) -> str:
    t = block.get("type", "")
    data = block.get(t) or {}
    text = plain(data.get("rich_text"))
    if t.startswith("heading_"):
        return "#" * int(t[-1]) + " " + text
    if t == "bulleted_list_item":
        return f"- {text}"
    if t == "numbered_list_item":
        return f"{number}. {text}"
    if t == "to_do":
        return f"- [{'x' if data.get('checked') else ' '}] {text}"
    if t in ("quote", "callout"):
        return f"> {text}"
    if t == "code":
        return f"```{data.get('language', '')}\n{text}\n```"
    if t == "divider":
        return "---"
    if t == "toggle":
        return f"▸ {text}"
    if t == "child_page":
        return f"[subpágina] {data.get('title', '')} (id {block.get('id')})"
    if t == "child_database":
        return f"[base de datos] {data.get('title', '')} (id {block.get('id')})"
    if t in ("bookmark", "embed", "link_preview"):
        return f"[{t}] {data.get('url', '')}"
    if t in ("image", "file", "pdf", "video"):
        src = (data.get("external") or data.get("file") or {}).get("url", "")
        return f"[{t}] {plain(data.get('caption'))} {src.split('?')[0]}".strip()
    if t == "table_row":
        return "| " + " | ".join(plain(c) for c in data.get("cells", [])) + " |"
    if t == "paragraph":
        return text
    return f"[{t}]" if not text else text


def prop_value(prop: dict) -> Any:
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return plain(v)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "multi_select":
        return [o.get("name") for o in v or []]
    if t == "date":
        if not v:
            return None
        return v["start"] if not v.get("end") else f"{v['start']} → {v['end']}"
    if t in ("checkbox", "number", "url", "email", "phone_number", "created_time",
             "last_edited_time"):
        return v
    if t == "people":
        return [p.get("name") or p.get("id") for p in v or []]
    if t == "relation":
        return [r.get("id") for r in v or []]
    if t == "formula":
        return (v or {}).get((v or {}).get("type", ""), None)
    return None


def brief(obj: dict) -> dict:
    parent = obj.get("parent") or {}
    return {"id": obj.get("id"), "type": obj.get("object"), "title": title_of(obj),
            "url": obj.get("url"), "last_edited": obj.get("last_edited_time"),
            "parent": parent.get(parent.get("type", ""), parent.get("type"))}


def row(page: dict) -> dict:
    return {"id": page.get("id"), "url": page.get("url"),
            **{k: prop_value(v) for k, v in (page.get("properties") or {}).items()}}


def schema(db: dict) -> dict:
    out: dict[str, Any] = {}
    for name, p in (db.get("properties") or {}).items():
        t = p["type"]
        entry: dict[str, Any] = {"type": t}
        if t in ("select", "multi_select"):
            entry["options"] = [o["name"] for o in p[t].get("options", [])]
        if t == "status":
            entry["options"] = [o["name"] for o in p[t].get("options", [])]
            entry["groups"] = {g["name"]: [o["name"] for o in p[t]["options"]
                                           if o["id"] in g.get("option_ids", [])]
                               for g in p[t].get("groups", [])}
        out[name] = entry
    return out


# --------------------------------------------------------------------------- escritura

def rich(text: str) -> list[dict]:
    """Texto con formato en línea mínimo → rich_text (troceado a 2000 caracteres)."""
    parts: list[dict] = []

    def add(content: str, link: str | None = None, **ann: bool) -> None:
        for i in range(0, len(content), MAX_TEXT):
            item: dict[str, Any] = {"type": "text", "text": {"content": content[i:i + MAX_TEXT]}}
            if link:
                item["text"]["link"] = {"url": link}
            if ann:
                item["annotations"] = dict(ann)
            parts.append(item)

    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            add(text[pos:m.start()])
        if m.group(1) is not None:
            add(m.group(1), bold=True)
        elif m.group(2) is not None:
            add(m.group(2), code=True)
        else:
            add(m.group(3), link=m.group(4))
        pos = m.end()
    if pos < len(text):
        add(text[pos:])
    return parts


def _block(kind: str, text: str, **extra: Any) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text), **extra}}


def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            lang = s[3:].strip() or "plain text"
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            text = "\n".join(code)
            blocks.append({"object": "block", "type": "code", "code": {
                "rich_text": [{"type": "text", "text": {"content": text[j:j + MAX_TEXT]}}
                              for j in range(0, max(len(text), 1), MAX_TEXT)],
                "language": lang}})
        elif not s:
            pass
        elif s in ("---", "***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif m := re.match(r"^(#{1,3})\s+(.*)$", s):
            blocks.append(_block(f"heading_{len(m.group(1))}", m.group(2)))
        elif m := re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)$", s):
            blocks.append(_block("to_do", m.group(2), checked=m.group(1).lower() == "x"))
        elif m := re.match(r"^[-*]\s+(.*)$", s):
            blocks.append(_block("bulleted_list_item", m.group(1)))
        elif m := re.match(r"^\d+[.)]\s+(.*)$", s):
            blocks.append(_block("numbered_list_item", m.group(1)))
        elif s.startswith(">"):
            blocks.append(_block("quote", s.lstrip("> ")))
        else:
            blocks.append(_block("paragraph", s))
        i += 1
    return blocks


class PropertyError(ValueError):
    pass


def to_notion_props(values: dict[str, Any], schema_props: dict[str, dict],
                    allow_new_options: bool = False) -> dict:
    """Valores simples → propiedades Notion según el esquema. Rechaza propiedades desconocidas,
    de solo lectura y (salvo allow_new_options) opciones que no existen en select/status."""
    out: dict[str, Any] = {}
    for name, val in values.items():
        if name not in schema_props:
            raise PropertyError(f"la propiedad '{name}' no existe (usa notion.database_schema)")
        p = schema_props[name]
        t = p["type"]
        options = [o["name"] for o in (p.get(t) or {}).get("options", [])]
        if t == "title":
            out[name] = {"title": rich(str(val))}
        elif t == "rich_text":
            out[name] = {"rich_text": rich(str(val)) if val else []}
        elif t in ("select", "status"):
            if val in (None, ""):
                if t == "status":
                    raise PropertyError(f"'{name}' (status) no se puede vaciar")
                out[name] = {"select": None}
                continue
            if val not in options and (t == "status" or not allow_new_options):
                raise PropertyError(f"'{val}' no es una opción de '{name}'. Opciones: {options}")
            out[name] = {t: {"name": val}}
        elif t == "multi_select":
            vals = [val] if isinstance(val, str) else list(val or [])
            bad = [v for v in vals if v not in options]
            if bad and not allow_new_options:
                raise PropertyError(f"{bad} no son opciones de '{name}'. Opciones: {options}")
            out[name] = {"multi_select": [{"name": v} for v in vals]}
        elif t == "date":
            if not val:
                out[name] = {"date": None}
            elif isinstance(val, dict):
                out[name] = {"date": {k: val[k] for k in ("start", "end") if val.get(k)}}
            else:
                if not re.match(r"^\d{4}-\d{2}-\d{2}", str(val)):
                    raise PropertyError(f"fecha inválida para '{name}' (usa AAAA-MM-DD)")
                out[name] = {"date": {"start": str(val)}}
        elif t == "checkbox":
            out[name] = {"checkbox": bool(val) if not isinstance(val, str)
                         else val.lower() in ("true", "sí", "si", "1", "yes")}
        elif t == "number":
            try:
                out[name] = {"number": None if val in (None, "") else float(val)}
            except (TypeError, ValueError) as exc:
                raise PropertyError(f"'{name}' debe ser un número") from exc
        elif t in ("url", "email", "phone_number"):
            out[name] = {t: str(val) if val else None}
        else:
            raise PropertyError(f"'{name}' es de tipo {t}: no editable desde aquí")
    return out
