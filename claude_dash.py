#!/usr/bin/env python3
"""claude_dash — um lugar só para ver todas as sessões do Claude Code.

Card verde = disponível (idle), amarelo = trabalhando (busy), vermelho = precisa de você
(waiting), azul = no shell (o Claude reporta `shell` quando a sessão está num comando de shell). Cada card mostra o título da sessão e o começo da última mensagem do Claude.
Clique no card foca a janela do Ptyxis daquela sessão.

Fonte de dados: ~/.claude/sessions/<pid>.json (o próprio Claude Code grava status, nome,
cwd e sessionId). Título e última mensagem vêm do transcript
(~/.claude/projects/<cwd>/<sessionId>.jsonl: linhas "custom-title", "ai-title" e "assistant").

Foco da janela: extensão GNOME "Activate Window By Title" (D-Bus), casando o título que o
Claude escreve na janela do Ptyxis ("✳ <título> — claude"). Sessões abertas com `claude`
normal, cada uma na sua janela do Ptyxis. Se estiverem em abas de uma mesma janela, o painel
foca a janela (o Ptyxis não deixa escolher a aba de fora).

    python3 claude_dash.py              # janela GTK
    python3 claude_dash.py --text       # imprime a tabela e sai
    python3 claude_dash.py --shot x.png # renderiza a janela em PNG e sai
"""
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
SESS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJ_DIR = os.path.join(HOME, ".claude", "projects")

# status gravado pelo Claude Code -> (rótulo, classe css)
STATUS = {
    "idle": ("Disponível", "idle"),
    "busy": ("Trabalhando", "busy"),
    "waiting": ("Precisa de você", "waiting"),
    "shell": ("No shell", "shell"),
}
ORDER = {"waiting": 0, "busy": 1, "idle": 2, "shell": 3}
BG_RETIRE_SECS = 3600     # o daemon aposenta job bg ocioso e sem terminal após 60 min (daemon.log: "retire ... idle 60m")
PINS_FILE = os.path.join(HOME, ".claude", "jobs", "pins.json")  # jobs fixados (Ctrl+T) não são aposentados
TAIL_BYTES = 768 * 1024   # quanto do fim do transcript é lido
EXCERPT_CHARS = 260       # tamanho do trecho da última mensagem

SHOT = None  # --shot <png>: salva a janela e sai
DBUS_DEST = "org.gnome.Shell"
DBUS_PATH = "/de/lucaswerkmeister/ActivateWindowByTitle"
DBUS_IFACE = "de.lucaswerkmeister.ActivateWindowByTitle"


# ----------------------------------------------------------------------------- dados
def _proc_start(pid):
    """starttime do /proc/<pid>/stat (campo 22); None se o processo não existe."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return rest[19]
    except (OSError, IndexError):
        return None


def _project_dir(cwd):
    return os.path.join(PROJ_DIR, re.sub(r"[^A-Za-z0-9]", "-", cwd))


def _excerpt(text):
    """Começo da mensagem, sem marcação markdown e sem quebras."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_`]{1,3}", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:EXCERPT_CHARS] + ("…" if len(t) > EXCERPT_CHARS else "")


_tx_cache = {}


def transcript_info(cwd, session_id):
    """(título, trecho da última mensagem do Claude, mensagem completa) a partir do fim do transcript."""
    return transcript_info_path(os.path.join(_project_dir(cwd), f"{session_id}.jsonl"))


def transcript_info_path(path):
    try:
        st = os.stat(path)
    except OSError:
        return None, None, None
    key = (st.st_mtime_ns, st.st_size)
    cached = _tx_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    with open(path, "rb") as f:
        f.seek(max(0, st.st_size - TAIL_BYTES))
        tail = f.read().decode("utf-8", "replace")
    custom = ai = last = None
    for line in tail.splitlines():
        if '"type":"custom-title"' in line or '"type":"ai-title"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "custom-title" and rec.get("customTitle"):
                custom = rec["customTitle"]
            elif rec.get("type") == "ai-title" and rec.get("aiTitle"):
                ai = rec["aiTitle"]
        elif '"type":"assistant"' in line and '"type":"text"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("isSidechain"):
                continue  # subagente
            content = rec.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        last = block["text"]
    info = (custom or ai, _excerpt(last) if last else None, last.strip() if last else None)
    _tx_cache[path] = (key, info)
    return info


def _attach_targets():
    """Nomes/ids passados a `claude attach` nos processos vivos."""
    targets = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 3 and argv[0].endswith(b"claude") and argv[1] == b"attach":
            targets.add(argv[2].decode("utf-8", "replace"))
    return targets


_UUID_RE = re.compile(r"^[0-9a-f-]{36}\.jsonl$")
_NOISE_RE = re.compile(r"^\s*<(local-command|command-name|command-message|system-reminder)")
_head_cache = {}
CLOSED_LIMIT = 80


def _transcript_head(path):
    """(cwd, primeiro prompt do usuário) lidos do começo do transcript; cache por mtime/tamanho."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    key = (st.st_mtime_ns, st.st_size)
    cached = _head_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    cwd = first = None
    with open(path, "rb") as f:
        head = f.read(96 * 1024).decode("utf-8", "replace")
    for line in head.splitlines():
        if '"type":"user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        cwd = cwd or rec.get("cwd")
        if rec.get("isSidechain"):
            continue
        content = rec.get("message", {}).get("content")
        text = content if isinstance(content, str) else next(
            (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"), "") \
            if isinstance(content, list) else ""
        if text and not _NOISE_RE.match(text):
            first = text
            break
    info = (cwd, first)
    _head_cache[path] = (key, info)
    return info


def read_closed(live_ids, limit=CLOSED_LIMIT):
    """Sessões encerradas (transcript sem processo vivo), mais recentes primeiro."""
    files = []
    try:
        for d in os.listdir(PROJ_DIR):
            pd = os.path.join(PROJ_DIR, d)
            if not os.path.isdir(pd):
                continue
            for name in os.listdir(pd):
                if _UUID_RE.match(name) and name[:-6] not in live_ids:
                    path = os.path.join(pd, name)
                    try:
                        files.append((os.path.getmtime(path), path))
                    except OSError:
                        pass
    except OSError:
        return []
    files.sort(reverse=True)
    out = []
    for mtime, path in files[:limit]:
        title, last, _full = transcript_info_path(path)
        cwd, first = _transcript_head(path)
        if not title and not first:
            continue  # sessão de um comando só (/model, /clear…), nada para retomar
        out.append({
            "session_id": os.path.basename(path)[:-6],
            "title": title or _excerpt(first)[:90],
            "last": last,
            "cwd": cwd or HOME,
            "mtime": mtime,
            "size": os.path.getsize(path),
        })
    return out


def read_sessions():
    """Sessões vivas, ordenadas: vermelho, amarelo, verde, cinza."""
    out = []
    attached = _attach_targets()
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESS_DIR, name)) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        pid = s.get("pid")
        if not pid:
            continue
        start = _proc_start(pid)
        if start is None or (s.get("procStart") and str(s["procStart"]) != start):
            continue  # morto ou pid reaproveitado
        if s.get("spare"):
            continue  # sessão reserva que o Agent View mantém ociosa
        status = s.get("status") or "unknown"
        label, css = STATUS.get(status, (status, "shell"))
        cwd = s.get("cwd") or HOME
        title, last, full = transcript_info(cwd, s["sessionId"]) if s.get("sessionId") else (None, None, None)
        out.append({
            "pid": pid,
            "name": s.get("name") or str(pid),
            "status": status,
            "label": label,
            "css": css,
            "kind": s.get("kind") or "interactive",
            "cwd": cwd,
            "session_id": s.get("sessionId"),
            "title": title,
            "last": last,
            "last_full": full,
            "detail": s.get("detail"),
            "since_ms": s.get("statusUpdatedAt") or s.get("updatedAt") or 0,
            "job_id": s.get("jobId"),
            "parked_job_id": s.get("parkedJobId"),
            "attached": (s.get("name") in attached) or (s.get("jobId") in attached),
        })
    # host que mandou a conversa para o background (/bg ou ←): quem trabalha é a sessão bg
    live_jobs = {s["job_id"] for s in out if s["job_id"]}
    out = [s for s in out if not (s["parked_job_id"] and s["parked_job_id"] in live_jobs)]
    # bg ocioso e sem terminal = "fechado" na prática: o daemon só o aposenta 60 min depois
    try:
        with open(PINS_FILE) as f:
            pinned = f.read()
    except OSError:
        pinned = ""
    for s in out:
        s["parked"] = s["kind"] != "interactive" and s["status"] == "idle" and not s["attached"]
        keep = bool(s["job_id"]) and s["job_id"] in pinned
        s["retire_at"] = s["since_ms"] / 1000 + BG_RETIRE_SECS if s["parked"] and s["since_ms"] and not keep else None
    out.sort(key=lambda s: (s["parked"], ORDER.get(s["status"], 9), s["name"]))
    return out


def age(since_ms):
    if not since_ms:
        return ""
    d = max(0, time.time() - since_ms / 1000)
    if d < 60:
        return f"há {int(d)} s"
    if d < 3600:
        return f"há {int(d // 60)} min"
    return f"há {d / 3600:.1f} h"


def retire_label(s):
    """'encerra sozinha em N min' para job bg ocioso sem terminal; None nos demais casos."""
    if not s.get("retire_at"):
        return None
    m = int((s["retire_at"] - time.time()) // 60)
    return "encerra em " + (f"{m} min" if m > 0 else "instantes")


def print_text():
    rows = read_sessions()
    if not rows:
        print("nenhuma sessão viva")
        return
    for s in rows:
        print(f"{s['status']:8} {s['pid']:>8} {s['name']:20} {age(s['since_ms']):>10}  "
              f"{s['title'] or '-'}  [{os.path.basename(s['cwd'])}]"
              f"{('  (bg, ' + ('anexada' if s['attached'] else 'sem terminal') + (', ' + retire_label(s) if retire_label(s) else '') + ')') if s['kind'] != 'interactive' else ''}")
        if s["last"]:
            print(f"{'':40}{s['last'][:110]}")


# ----------------------------------------------------------------------------- foco
def activate(method, arg):
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    res = bus.call_sync(DBUS_DEST, DBUS_PATH, DBUS_IFACE, method,
                        GLib.Variant("(s)", (arg,)), GLib.VariantType("(b)"),
                        Gio.DBusCallFlags.NONE, 2000, None)
    return bool(res.unpack()[0])


def focus_session(s):
    """Devolve (ok, mensagem). Mensagem só quando algo precisa ser dito."""
    needle = s["title"] or s["name"]
    if s["kind"] != "interactive":
        try:
            if activate("activateBySubstring", s["name"]):
                return True, None
        except Exception:
            pass
        subprocess.Popen(["ptyxis", "--new-window", "-d", s["cwd"], "--", "claude", "attach", s["name"]])
        return True, f"abrindo attach de «{s['name']}» em janela nova"
    try:
        if activate("activateBySubstring", needle):
            return True, None
        if activate("activateByWmClass", "org.gnome.Ptyxis"):
            return True, f"nenhuma janela com «{needle}»; foquei o Ptyxis"
        return False, "nenhuma janela do Ptyxis aberta"
    except Exception as e:  # extensão fora, shell sem a interface
        msg = str(e)
        if "UnknownMethod" in msg or "UnknownObject" in msg or "UnknownInterface" in msg or "No such" in msg:
            return False, "extensão Activate Window By Title não está ativa"
        return False, f"erro ao focar: {msg[:120]}"


# ----------------------------------------------------------------------------- GTK
CSS = b"""
flowboxchild.sess { border-radius: 12px; padding: 12px 14px; margin: 0;
                    border-left: 8px solid @borders; background: @card_bg_color;
                    color: @window_fg_color; }
flowboxchild.sess:hover { filter: brightness(1.06); }
flowboxchild.sess.parked { opacity: 0.55; }
flowboxchild.sess.parked:hover { opacity: 1; }
flowboxchild.sess:backdrop, flowboxchild.sess:backdrop label { color: @window_fg_color; opacity: 1; }
flowboxchild.st-idle    { border-left-color: @success_color; background: alpha(@success_color, 0.22); }
flowboxchild.st-busy    { border-left-color: @warning_color; background: alpha(@warning_color, 0.28); }
flowboxchild.st-waiting { border-left-color: @error_color;   background: alpha(@error_color,   0.30); }
flowboxchild.st-shell   { border-left-color: @blue_2; background: alpha(@blue_2, 0.22); }
flowboxchild.sess label.title { font-weight: 800; font-size: 1.08em; }
flowboxchild.sess button.expand { min-width: 22px; min-height: 22px; padding: 2px; opacity: 0.7; margin-top: -4px; margin-right: -6px; }
flowboxchild.sess button.expand:hover { opacity: 1; }
.scrim { background: alpha(black, 0.6); }
.detail { border-radius: 16px; padding: 20px 24px 18px 24px; border-left: 10px solid @borders;
          background: @window_bg_color; color: @window_fg_color;
          box-shadow: 0 10px 40px alpha(black, 0.55), 0 0 0 1px alpha(@window_fg_color, 0.08); }
.detail.st-idle    { border-left-color: @success_color; background: mix(@window_bg_color, @success_color, 0.22); }
.detail.st-busy    { border-left-color: @warning_color; background: mix(@window_bg_color, @warning_color, 0.28); }
.detail.st-waiting { border-left-color: @error_color;   background: mix(@window_bg_color, @error_color,   0.30); }
.detail.st-shell   { border-left-color: @blue_2; background: mix(@window_bg_color, @blue_2, 0.24); }
.detail label.title { font-weight: 900; font-size: 1.35em; }
.detail label.dim   { color: alpha(@window_fg_color, 0.65); font-size: 0.9em; }
.detail label.status-idle    { color: @success_color; font-weight: 600; }
.detail label.status-busy    { color: @warning_color; font-weight: 600; }
.detail label.status-waiting { color: @error_color;   font-weight: 600; }
.detail label.status-shell   { color: @blue_2; font-weight: 600; }
.detail label.message { font-size: 1.02em; line-height: 1.45; }
.drawer { background: @window_bg_color; border-left: 1px solid alpha(@window_fg_color, 0.12); }
.drawer label.title { font-weight: 800; font-size: 1.04em; }
.drawer label.dim { color: alpha(@window_fg_color, 0.6); font-size: 0.88em; }
.drawer label.last { color: alpha(@window_fg_color, 0.8); font-size: 0.9em; }
list.closed { background: transparent; }
list.closed row.closed-row { border-radius: 12px; padding: 12px 14px; margin: 5px 4px;
                                background: alpha(@window_fg_color, 0.07);
                                border-left: 7px solid alpha(@window_fg_color, 0.35);
                                box-shadow: 0 1px 3px alpha(black, 0.35); }
list.closed row.closed-row:hover { background: alpha(@window_fg_color, 0.13); }
flowboxchild.sess label.dim   { color: alpha(@window_fg_color, 0.65); font-size: 0.9em; }
flowboxchild.sess label.last  { color: alpha(@window_fg_color, 0.85); font-size: 0.92em; }
flowboxchild.sess label.status-idle,    flowboxchild.sess:backdrop label.status-idle    { color: @success_color; font-weight: 600; }
flowboxchild.sess label.status-busy,    flowboxchild.sess:backdrop label.status-busy    { color: @warning_color; font-weight: 600; }
flowboxchild.sess label.status-waiting, flowboxchild.sess:backdrop label.status-waiting { color: @error_color;   font-weight: 600; }
flowboxchild.sess label.status-shell,   flowboxchild.sess:backdrop label.status-shell   { color: @blue_2; font-weight: 600; }
"""


def run_gui():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

    class Card(Gtk.FlowBoxChild):
        def __init__(self, s, on_expand, on_stop):
            super().__init__()
            self.add_css_class("sess")
            self.session = s
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            top = Gtk.Box(spacing=6)
            self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["title"], hexpand=True)
            self.expand = Gtk.Button(icon_name="pan-down-symbolic", tooltip_text="Ler a mensagem inteira",
                                     css_classes=["flat", "circular", "expand"], valign=Gtk.Align.START)
            self.expand.connect("clicked", lambda _b: on_expand(self.session))
            self.close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular", "expand"],
                                    tooltip_text="Encerrar (claude stop): sem terminal e ocioso; a conversa fica salva",
                                    valign=Gtk.Align.START)
            self.close.connect("clicked", lambda _b: on_stop(self.session))
            top.append(self.title)
            top.append(self.expand)
            top.append(self.close)
            line = Gtk.Box(spacing=6)
            self.status = Gtk.Label(xalign=0)
            self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim"], hexpand=True)
            line.append(self.status)
            line.append(self.sub)
            self.last = Gtk.Label(xalign=0, yalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                  lines=3, ellipsize=Pango.EllipsizeMode.END, css_classes=["last"],
                                  margin_top=4, vexpand=True)
            box.append(top)
            box.append(line)
            box.append(self.last)
            self.set_child(box)
            self.update(s)

        def update(self, s):
            self.session = s
            for c in list(self.get_css_classes()):
                if c.startswith("st-"):
                    self.remove_css_class(c)
            self.add_css_class(f"st-{s['css']}")
            if s["parked"]:
                self.add_css_class("parked")
            else:
                self.remove_css_class("parked")
            self.close.set_visible(s["parked"] and bool(s["job_id"]))
            title = s["title"] or s["name"]
            self.title.set_text(title)
            self.status.set_text(f"● {s['label']} · {age(s['since_ms'])}")
            self.status.set_css_classes([f"status-{s['css']}"])
            parts = []
            if s["name"] != title:
                parts.append(s["name"])
            parts.append(os.path.basename(s["cwd"]) or s["cwd"])
            if s["kind"] != "interactive":
                parts.append(("sem terminal" if s["parked"] else "background · " + ("terminal anexado" if s["attached"] else "sem terminal")))
            if retire_label(s):
                parts.append(retire_label(s))
            if s.get("detail"):
                parts.append(s["detail"])
            self.sub.set_text("· " + " · ".join(parts))
            self.last.set_text(s["last"] or "")
            self.last.set_visible(bool(s["last"]))
            self.expand.set_visible(bool(s["last_full"]))

    class Win(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Claude Dash", default_width=760, default_height=520)
            self.cards = {}
            self.wtitle = Adw.WindowTitle(title="Claude Dash")
            header = Adw.HeaderBar(title_widget=self.wtitle)
            new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Nova sessão (janela do Ptyxis com claude)")
            new_btn.connect("clicked", self.on_new)
            header.pack_start(new_btn)
            self.drawer_btn = Gtk.ToggleButton(icon_name="document-open-recent-symbolic",
                                               tooltip_text="Sessões encerradas (retomar)")
            self.drawer_btn.connect("toggled", lambda b: self.toggle_drawer(b.get_active()))
            header.pack_end(self.drawer_btn)
            self.set_icon_name("dev.araripe.ClaudeDash")
            self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True,
                                    min_children_per_line=2, max_children_per_line=2, homogeneous=True,
                                    row_spacing=10, column_spacing=10, valign=Gtk.Align.START,
                                    margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
            self.flow.set_sort_func(lambda a, b: (a.session["parked"], ORDER.get(a.session["status"], 9), a.session["name"])
                                    > (b.session["parked"], ORDER.get(b.session["status"], 9), b.session["name"]))
            self.flow.connect("child-activated", self.on_card)
            self.empty = Adw.StatusPage(title="Nenhuma sessão viva", icon_name="utilities-terminal-symbolic",
                                        description="Abra uma com o botão + ou rode `claude` num terminal.")
            self.stack = Gtk.Stack()
            scroller = Gtk.ScrolledWindow(child=self.flow, vexpand=True)
            scroller.add_css_class("background")  # pinta o fundo (a captura --shot não inclui o da janela)
            self.stack.add_named(scroller, "list")
            self.stack.add_named(self.empty, "empty")
            # detalhe do card: fundo escurecido (clique fora fecha) + painel com a mesma estética
            self.dim = Gtk.Box(css_classes=["scrim"], visible=False, hexpand=True, vexpand=True)
            self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["detail"],
                                  halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=640)
            self.d_title = Gtk.Label(xalign=0, wrap=True, css_classes=["title"])
            d_line = Gtk.Box(spacing=6)
            self.d_status = Gtk.Label(xalign=0)
            self.d_sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim"], hexpand=True)
            d_line.append(self.d_status)
            d_line.append(self.d_sub)
            self.d_text = Gtk.Label(xalign=0, yalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                    selectable=True, css_classes=["message"], margin_end=6)
            d_scroll = Gtk.ScrolledWindow(child=self.d_text, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          propagate_natural_height=True, max_content_height=480,
                                          margin_top=10, margin_bottom=6)
            d_btns = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6)
            self.d_stop = Gtk.Button(label="Encerrar sessão", css_classes=["destructive-action"],
                                     tooltip_text="claude stop: para a sessão em background (a conversa fica salva)")
            self.d_stop.connect("clicked", self.on_detail_stop)
            d_btns.append(self.d_stop)
            d_close = Gtk.Button(label="Fechar")
            d_close.connect("clicked", lambda _b: self.hide_detail())
            d_go = Gtk.Button(label="Ir para a janela", css_classes=["suggested-action"])
            d_go.connect("clicked", self.on_detail_go)
            d_btns.append(d_close)
            d_btns.append(d_go)
            for w in (self.d_title, d_line, d_scroll, d_btns):
                self.detail.append(w)
            self.dim.append(self.detail)
            click = Gtk.GestureClick()
            click.connect("released", self.on_dim_click)
            self.dim.add_controller(click)
            self.overlay = Gtk.Overlay(child=self.stack)
            self.overlay.add_overlay(self.dim)
            # drawer lateral: sessões encerradas, clique retoma numa janela nova do Ptyxis
            self.drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["drawer"], width_request=520)
            d_head = Gtk.Box(spacing=6, margin_top=10, margin_bottom=6, margin_start=12, margin_end=8)
            d_head.append(Gtk.Label(label="Encerradas", xalign=0, hexpand=True, css_classes=["title"]))
            d_x = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"])
            d_x.connect("clicked", lambda _b: self.toggle_drawer(False))
            d_head.append(d_x)
            self.search = Gtk.SearchEntry(placeholder_text="filtrar por título", margin_start=12, margin_end=12,
                                          margin_bottom=6)
            self.search.connect("search-changed", lambda _e: self.closed_list.invalidate_filter())
            self.closed_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True,
                                           css_classes=["closed"], margin_start=8, margin_end=8, margin_bottom=8)
            self.closed_list.set_filter_func(self.closed_filter)
            self.closed_list.connect("row-activated", self.on_closed_row)
            self.drawer.append(d_head)
            self.drawer.append(self.search)
            self.drawer.append(Gtk.ScrolledWindow(child=self.closed_list, vexpand=True,
                                                  hscrollbar_policy=Gtk.PolicyType.NEVER))
            self.split = Adw.OverlaySplitView(content=self.overlay, sidebar=self.drawer, collapsed=True,
                                              show_sidebar=False, sidebar_position=Gtk.PackType.END,
                                              max_sidebar_width=600, min_sidebar_width=480)
            self.toasts = Adw.ToastOverlay(child=self.split)
            view = Adw.ToolbarView(content=self.toasts)
            view.add_top_bar(header)
            self.set_content(view)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.on_key)
            self.add_controller(keys)
            self.refresh()
            GLib.timeout_add(1500, self.refresh)
            if SHOT:
                if "--open" in sys.argv:
                    GLib.timeout_add(600, lambda: (self.show_message(self.flow.get_child_at_index(0).session), False)[1])
                if "--drawer" in sys.argv:
                    GLib.timeout_add(600, lambda: (self.toggle_drawer(True), False)[1])
                GLib.timeout_add(2500, self.shot)
            try:  # status muda -> atualiza na hora
                self.mon = Gio.File.new_for_path(SESS_DIR).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                self.mon.connect("changed", lambda *_: self.refresh())
            except Exception:
                pass

        def refresh(self):
            rows = read_sessions()
            seen = set()
            for s in rows:
                seen.add(s["pid"])
                if s["pid"] in self.cards:
                    self.cards[s["pid"]].update(s)
                else:
                    self.cards[s["pid"]] = Card(s, self.show_message, self.stop_job)
                    self.flow.append(self.cards[s["pid"]])
            for pid in list(self.cards):
                if pid not in seen:
                    self.flow.remove(self.cards.pop(pid))
            self.flow.invalidate_sort()
            if getattr(self, "detail_pid", None) in self.cards:
                self.fill_detail(self.cards[self.detail_pid].session)
            n = {}
            for s in rows:
                key = "parked" if s["parked"] else s["status"]
                n[key] = n.get(key, 0) + 1
            bits = []
            if n.get("waiting"):
                bits.append(f"{n['waiting']} esperando você")
            if n.get("busy"):
                bits.append(f"{n['busy']} trabalhando")
            if n.get("idle"):
                bits.append(f"{n['idle']} livre{'s' if n['idle'] > 1 else ''}")
            if n.get("parked"):
                bits.append(f"{n['parked']} sem terminal")
            self.wtitle.set_subtitle(" · ".join(bits) if bits else "")
            self.stack.set_visible_child_name("list" if rows else "empty")
            return True

        def shot(self):
            """Renderiza o conteúdo direto do nó de cena (não depende de frame do compositor)."""
            from gi.repository import Graphene
            root = self.split
            w, h = root.get_width(), root.get_height()
            if w == 0 or h == 0:
                return True
            snap = Gtk.Snapshot()
            snap.append_color(Gdk.RGBA(red=0.14, green=0.14, blue=0.14, alpha=1), Graphene.Rect().init(0, 0, w, h))

            def paint(widget):
                ok, b = widget.compute_bounds(root)
                if not ok:
                    return
                snap.save()
                snap.translate(Graphene.Point().init(b.origin.x, b.origin.y))
                widget.get_parent().snapshot_child(widget, snap)
                snap.restore()
            paint(self.stack)
            if self.dim.get_visible():
                paint(self.dim)
            if self.split.get_show_sidebar():
                paint(self.drawer)
            node = snap.to_node()
            if node is None:
                return True
            tex = self.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(SHOT)
            self.get_application().quit()
            return False

        def show_message(self, s):
            self.detail_pid = s["pid"]
            self.fill_detail(s)
            self.dim.set_visible(True)

        def fill_detail(self, s):
            for c in list(self.detail.get_css_classes()):
                if c.startswith("st-"):
                    self.detail.remove_css_class(c)
            self.detail.add_css_class(f"st-{s['css']}")
            title = s["title"] or s["name"]
            self.d_title.set_text(title)
            self.d_status.set_text(f"● {s['label']} · {age(s['since_ms'])}")
            self.d_status.set_css_classes([f"status-{s['css']}"])
            parts = [p for p in (s["name"] if s["name"] != title else None,
                                 os.path.basename(s["cwd"]) or s["cwd"],
                                 ("background · " + ("terminal anexado" if s["attached"] else "sem terminal"))
                                 if s["kind"] != "interactive" else None,
                                 retire_label(s),
                                 s.get("detail")) if p]
            self.d_sub.set_text("· " + " · ".join(parts))
            self.d_stop.set_visible(s["kind"] != "interactive" and bool(s["job_id"]))
            if self.d_text.get_text() != (s["last_full"] or ""):
                self.d_text.set_text(s["last_full"] or "(sem mensagem ainda)")

        def hide_detail(self):
            self.dim.set_visible(False)
            self.detail_pid = None

        def on_dim_click(self, gesture, _n, x, y):
            hit = self.dim.pick(x, y, Gtk.PickFlags.DEFAULT)
            while hit is not None and hit is not self.dim:
                if hit is self.detail:
                    return  # clique dentro do painel
                hit = hit.get_parent()
            self.hide_detail()

        def on_detail_stop(self, _b):
            card = self.cards.get(self.detail_pid)
            self.hide_detail()
            if not card:
                return
            self.stop_job(card.session)

        def stop_job(self, s):
            subprocess.Popen(["claude", "stop", s["job_id"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.toasts.add_toast(Adw.Toast(title=f"encerrando «{s['name']}» (claude stop)", timeout=4))

        def on_detail_go(self, _b):
            card = self.cards.get(self.detail_pid)
            self.hide_detail()
            if card:
                self.on_card(self.flow, card)

        def toggle_drawer(self, show):
            if show:
                self.load_closed()
            self.split.set_show_sidebar(show)
            if self.drawer_btn.get_active() != show:
                self.drawer_btn.set_active(show)
            if show:
                self.search.grab_focus()

        def load_closed(self):
            live = {s["session_id"] for s in read_sessions() if s["session_id"]}
            while (row := self.closed_list.get_row_at_index(0)) is not None:
                self.closed_list.remove(row)
            for c in read_closed(live):
                row = Gtk.ListBoxRow(activatable=True, css_classes=["closed-row"])
                row.closed = c
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                box.append(Gtk.Label(label=c["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                     css_classes=["title"]))
                when = time.strftime("%d/%m %H:%M", time.localtime(c["mtime"]))
                box.append(Gtk.Label(label=f"{when} · {os.path.basename(c['cwd']) or c['cwd']} · {c['size'] / 1e6:.1f} MB",
                                     xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim"]))
                if c["last"]:
                    box.append(Gtk.Label(label=c["last"], xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                         lines=2, ellipsize=Pango.EllipsizeMode.END, css_classes=["last"]))
                row.set_child(box)
                self.closed_list.append(row)

        def closed_filter(self, row):
            q = self.search.get_text().strip().lower()
            if not q:
                return True
            c = row.closed
            return q in c["title"].lower() or q in (c["last"] or "").lower()

        def on_closed_row(self, _list, row):
            c = row.closed
            subprocess.Popen(["ptyxis", "--new-window", "-d", c["cwd"], "--", "claude", "--resume", c["session_id"]])
            self.toasts.add_toast(Adw.Toast(title=f"retomando «{c['title'][:60]}» em janela nova", timeout=4))
            self.toggle_drawer(False)

        def on_new(self, _btn):
            subprocess.Popen(["ptyxis", "--new-window", "-d", HOME, "--", "claude"])

        def on_card(self, _flow, child):
            ok, msg = focus_session(child.session)
            if msg:
                self.toasts.add_toast(Adw.Toast(title=msg, timeout=4))

        def on_key(self, _c, keyval, _code, _state):
            if self.search.has_focus():
                if keyval == Gdk.KEY_Escape:
                    self.toggle_drawer(False)
                    return True
                return False
            if keyval == Gdk.KEY_e:
                self.toggle_drawer(not self.split.get_show_sidebar())
                return True
            if keyval == Gdk.KEY_Escape and self.split.get_show_sidebar():
                self.toggle_drawer(False)
                return True
            if Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
                child = self.flow.get_child_at_index(keyval - Gdk.KEY_1)
                if child:
                    self.on_card(self.flow, child)
                return True
            if keyval == Gdk.KEY_n:
                self.on_new(None)
                return True
            if keyval == Gdk.KEY_Escape and self.dim.get_visible():
                self.hide_detail()
                return True
            if keyval == Gdk.KEY_q or keyval == Gdk.KEY_Escape:
                self.close()
                return True
            return False

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="dev.araripe.ClaudeDash")

        def do_activate(self):
            prov = Gtk.CssProvider()
            prov.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, 800)
            win = self.props.active_window or Win(self)
            win.maximize()
            win.present()

    sys.exit(App().run([sys.argv[0]]))


if __name__ == "__main__":
    if "--text" in sys.argv:
        print_text()
    else:
        if "--shot" in sys.argv:
            SHOT = sys.argv[sys.argv.index("--shot") + 1]
        run_gui()
