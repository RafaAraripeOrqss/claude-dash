#!/usr/bin/env python3
"""claude_dash — um lugar só para ver todas as sessões do Claude Code.

Card verde = disponível (idle), amarelo = trabalhando (busy), vermelho = precisa de você
(waiting), azul = no shell (o Claude reporta `shell` quando a sessão está num comando de shell). Cada card mostra o título da sessão e o começo da última mensagem do Claude.
Clique no card foca a janela do Ptyxis daquela sessão.

Contas: o painel junta as contas do Claude Code listadas em ACCOUNTS (nome, pasta de config).
Hoje: Trabalho = ~/.claude (comando `claude`) e Pessoal = ~/.claude-pessoal (`claude-p`, que roda
com CLAUDE_CONFIG_DIR=~/.claude-pessoal). Cada conta tem a sua aba; o rótulo mostra quantas
sessões vivas ela tem e um ponto vermelho aparece quando a aba que você NÃO está olhando tem
sessão esperando por você. O drawer de encerradas é por aba. Pasta ausente ou sem sessions/ =
conta vazia, sem erro.

Fonte de dados (por conta, dentro da pasta de config dela): <pasta>/sessions/<pid>.json (o próprio
Claude Code grava status, nome, cwd e sessionId). Título e última mensagem vêm do transcript
(<pasta>/projects/<cwd>/<sessionId>.jsonl: linhas "custom-title", "ai-title" e "assistant"); os jobs
fixados vêm de <pasta>/jobs/pins.json.

Foco da janela: extensão GNOME "Activate Window By Title" (D-Bus), casando o título que o
Claude escreve na janela do Ptyxis ("✳ <título> — claude"). Sessões abertas com `claude`
normal, cada uma na sua janela do Ptyxis. Se estiverem em abas de uma mesma janela, o painel
foca a janela (o Ptyxis não deixa escolher a aba de fora).

    python3 claude_dash.py              # janela GTK
    python3 claude_dash.py --text       # imprime a tabela (agrupada por conta) e sai
    python3 claude_dash.py --shot x.png # renderiza a janela em PNG e sai (--account <nome> escolhe a aba)

Só para teste: CLAUDE_DASH_ACCOUNTS="Nome=/pasta;Outro=/pasta" substitui a lista ACCOUNTS.
"""
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DEFAULT_CFG = os.path.join(HOME, ".claude")

# Contas do Claude Code: (nome da aba, pasta de config). Edite aqui para somar ou tirar uma conta.
ACCOUNTS = [
    ("Trabalho", os.path.join(HOME, ".claude")),          # comando `claude`
    ("Pessoal", os.path.join(HOME, ".claude-pessoal")),   # comando `claude-p` (CLAUDE_CONFIG_DIR)
]
if os.environ.get("CLAUDE_DASH_ACCOUNTS"):  # só para teste: "Nome=/pasta;Outro=/pasta"
    ACCOUNTS = [(n, os.path.expanduser(d)) for n, _, d in
                (item.partition("=") for item in os.environ["CLAUDE_DASH_ACCOUNTS"].split(";") if item)]


def sess_dir(cfg):
    return os.path.join(cfg, "sessions")


def proj_dir(cfg):
    return os.path.join(cfg, "projects")


def pins_file(cfg):
    return os.path.join(cfg, "jobs", "pins.json")  # jobs fixados (Ctrl+T) não são aposentados


# O ptyxis tem uma instância só: `ptyxis --new-window -- cmd` roda o cmd com o ambiente de quem abriu a
# primeira janela, não o do painel. Se foi um lembrete do systemd disparado no login (16/11), o PATH é o
# do systemd, sem ~/.local/bin, e o `claude` dá "comando não encontrado". Por isso o PATH vai explícito.
# WAYLAND_DISPLAY/DISPLAY também: sem elas o Claude não acha a área de transferência (selecionar não copia).
DISPLAY_ENV = [f"{k}={os.environ[k]}" for k in ("WAYLAND_DISPLAY", "DISPLAY") if os.environ.get(k)]
LOCAL_BIN = os.path.join(HOME, ".local", "bin")
_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
CLAUDE_PATH = _path if LOCAL_BIN in _path.split(os.pathsep) else LOCAL_BIN + os.pathsep + _path


def claude_cmd(cfg, *args):
    """Linha de comando do `claude` na conta da pasta cfg, com o PATH do painel (a conta padrão dispensa CLAUDE_CONFIG_DIR)."""
    pre = ["env", f"PATH={CLAUDE_PATH}", *DISPLAY_ENV]
    if os.path.abspath(cfg) != DEFAULT_CFG:
        pre.append(f"CLAUDE_CONFIG_DIR={cfg}")
    return pre + ["claude", *args]


# Flags de sessão interativa por conta, iguais às do comando de terminal (o `claude-p` abre em sonnet).
SESSION_ARGS = {os.path.join(HOME, ".claude-pessoal"): ["--model", "sonnet"]}


def claude_session_cmd(cfg, *args):
    """Como claude_cmd, para abrir/retomar sessão: acrescenta as flags da conta (não serve para subcomando)."""
    return claude_cmd(cfg, *SESSION_ARGS.get(os.path.abspath(cfg), []), *args)


# status gravado pelo Claude Code -> (rótulo, classe css)
STATUS = {
    "idle": ("Disponível", "idle"),
    "busy": ("Trabalhando", "busy"),
    "waiting": ("Precisa de você", "waiting"),
    "shell": ("No shell", "shell"),
}
ORDER = {"waiting": 0, "busy": 1, "idle": 2, "shell": 3}
BG_RETIRE_SECS = 3600     # o daemon aposenta job bg ocioso e sem terminal após 60 min (daemon.log: "retire ... idle 60m")
TAIL_CHUNK = 1024 * 1024  # bloco lido do fim do transcript, recuando até achar a última mensagem
SCAN_CAP = 64 * 1024 * 1024  # teto da primeira leitura de um transcript (depois só lê o que entrou)
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


def _project_dir(cfg, cwd):
    return os.path.join(proj_dir(cfg), re.sub(r"[^A-Za-z0-9]", "-", cwd))


def _excerpt(text):
    """Começo da mensagem, sem marcação markdown e sem quebras."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_`]{1,3}", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:EXCERPT_CHARS] + ("…" if len(t) > EXCERPT_CHARS else "")


_tx_cache = {}


_tx_where = {}


def _transcript_path(cfg, cwd, session_id):
    """Transcript da sessão. A pasta é a do cwd em que a conversa começou: se ela mudou de pasta
    ou foi retomada de outra, o cwd atual não bate e o arquivo é procurado nas demais pastas."""
    name = f"{session_id}.jsonl"
    path = os.path.join(_project_dir(cfg, cwd), name)
    if os.path.exists(path):
        return path
    cached = _tx_where.get((cfg, session_id))
    if cached and os.path.exists(cached):
        return cached
    try:
        for d in os.listdir(proj_dir(cfg)):
            cand = os.path.join(proj_dir(cfg), d, name)
            if os.path.exists(cand):
                _tx_where[(cfg, session_id)] = cand
                return cand
    except OSError:
        pass
    return path


def transcript_info(cfg, cwd, session_id):
    """(título, trecho da última mensagem do Claude, mensagem completa) a partir do fim do transcript."""
    return transcript_info_path(_transcript_path(cfg, cwd, session_id))


def _scan_lines(lines):
    """(custom-title, ai-title, texto do Claude) mais recentes num trecho de linhas jsonl (bytes)."""
    custom = ai = last = None
    for line in lines:
        if b'"type":"custom-title"' in line or b'"type":"ai-title"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "custom-title" and rec.get("customTitle"):
                custom = rec["customTitle"]
            elif rec.get("type") == "ai-title" and rec.get("aiTitle"):
                ai = rec["aiTitle"]
        elif b'"type":"assistant"' in line and b'"type":"text"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "assistant" or rec.get("isSidechain"):
                continue  # subagente, ou registro que só cita uma mensagem
            content = rec.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                        last = block["text"]
    return custom, ai, last


def _scan_back(f, size):
    """Lê o transcript do fim para o começo, em blocos, até achar a última mensagem do Claude e um
    título. Uma janela fixa não basta: desde o 2.1.31x o transcript grava os requests da API
    (api-request-blob/-shape) e anexos renderizados, e uma sequência longa de ferramentas empurra
    a última mensagem para mais de 1 MB do fim. Devolve (custom, ai, last, fim da última linha completa)."""
    custom = ai = last = None
    pos, carry, read, end = size, b"", 0, None
    while pos > 0 and read < SCAN_CAP:
        start = max(0, pos - TAIL_CHUNK)
        f.seek(start)
        raw = f.read(pos - start)
        read += len(raw)
        if end is None:
            nl = raw.rfind(b"\n")
            end = start + nl + 1 if nl >= 0 else start
        block, pos = raw + carry, start
        if start > 0:
            cut = block.find(b"\n")
            if cut < 0:
                carry = block
                continue
            carry, block = block[:cut], block[cut + 1:]
        c, a, l = _scan_lines(block.split(b"\n"))
        custom, ai, last = custom or c, ai or a, last or l
        if last and (custom or ai):
            break
    return custom, ai, last, end or 0


def transcript_info_path(path):
    try:
        st = os.stat(path)
    except OSError:
        return None, None, None
    key = (st.st_mtime_ns, st.st_size)
    cached = _tx_cache.get(path)
    if cached and cached["key"] == key:
        return cached["info"]
    with open(path, "rb") as f:
        if cached and st.st_size >= cached["end"]:
            # o transcript só cresce: lê o que entrou desde a última leitura
            f.seek(cached["end"])
            new = f.read(st.st_size - cached["end"])
            nl = new.rfind(b"\n")
            c, a, l = _scan_lines(new[:nl + 1].split(b"\n")) if nl >= 0 else (None, None, None)
            custom, ai, last = c or cached["custom"], a or cached["ai"], l or cached["last"]
            end = cached["end"] + nl + 1
        else:
            custom, ai, last, end = _scan_back(f, st.st_size)
    info = (custom or ai, _excerpt(last) if last else None, last.strip() if last else None)
    _tx_cache[path] = {"key": key, "end": end, "custom": custom, "ai": ai, "last": last, "info": info}
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


def read_closed(cfg, live_ids, limit=CLOSED_LIMIT):
    """Sessões encerradas da conta (transcript sem processo vivo), mais recentes primeiro."""
    files = []
    try:
        for d in os.listdir(proj_dir(cfg)):
            pd = os.path.join(proj_dir(cfg), d)
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


def read_sessions(cfg, attached=None):
    """Sessões vivas da conta, ordenadas: vermelho, amarelo, verde, cinza. Pasta ausente = lista vazia."""
    out = []
    if attached is None:
        attached = _attach_targets()
    try:
        names = os.listdir(sess_dir(cfg))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sess_dir(cfg), name)) as f:
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
        if s.get("entrypoint") == "sdk-cli" and (s.get("kind") or "interactive") == "interactive":
            continue  # `claude -p` (auditoria, hook, script): sem terminal e sem conversa, nada para focar
        status = s.get("status") or "unknown"
        label, css = STATUS.get(status, (status, "shell"))
        cwd = s.get("cwd") or HOME
        title, last, full = transcript_info(cfg, cwd, s["sessionId"]) if s.get("sessionId") else (None, None, None)
        out.append({
            "cfg": cfg,
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
        with open(pins_file(cfg)) as f:
            pinned = f.read()
    except OSError:
        pinned = ""
    for s in out:
        s["parked"] = s["kind"] != "interactive" and s["status"] == "idle" and not s["attached"]
        keep = bool(s["job_id"]) and s["job_id"] in pinned
        s["retire_at"] = s["since_ms"] / 1000 + BG_RETIRE_SECS if s["parked"] and s["since_ms"] and not keep else None
    out.sort(key=lambda s: (s["parked"], ORDER.get(s["status"], 9), s["name"]))
    return out


def read_accounts():
    """[(nome, pasta, sessões vivas)] na ordem de ACCOUNTS."""
    attached = _attach_targets()
    return [(name, cfg, read_sessions(cfg, attached)) for name, cfg in ACCOUNTS]


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
    for i, (name, _cfg, rows) in enumerate(read_accounts()):
        if i:
            print()
        print(f"== {name} ({len(rows)}) ==")
        if not rows:
            print("nenhuma sessão viva")
        _print_rows(rows)


def _print_rows(rows):
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
        subprocess.Popen(["ptyxis", "--new-window", "-d", s["cwd"], "--"] + claude_cmd(s["cfg"], "attach", s["name"]))
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
.tab-dot { color: @error_color; font-size: 0.8em; }
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

    class AccountPage(Gtk.Box):
        """Uma conta: grade de cards, detalhe, drawer de encerradas e toasts, tudo da própria pasta de config."""

        def __init__(self, win, name, cfg):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
            self.win, self.account, self.cfg = win, name, cfg
            self.cards = {}
            self.rows = []
            self.detail_pid = None
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
            # drawer lateral: sessões encerradas da conta, clique retoma numa janela nova do Ptyxis
            self.drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["drawer"], width_request=520)
            d_head = Gtk.Box(spacing=6, margin_top=10, margin_bottom=6, margin_start=12, margin_end=8)
            d_head.append(Gtk.Label(label=f"Encerradas · {name}", xalign=0, hexpand=True, css_classes=["title"]))
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
            self.toasts = Adw.ToastOverlay(child=self.split, hexpand=True, vexpand=True)
            self.append(self.toasts)

        def toast(self, title):
            self.toasts.add_toast(Adw.Toast(title=title, timeout=4))

        def refresh(self, rows):
            self.rows = rows
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
            if self.detail_pid in self.cards:
                self.fill_detail(self.cards[self.detail_pid].session)
            elif self.detail_pid is not None:
                self.hide_detail()
            self.stack.set_visible_child_name("list" if rows else "empty")

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
            subprocess.Popen(claude_cmd(self.cfg, "stop", s["job_id"]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.toast(f"encerrando «{s['name']}» (claude stop)")

        def on_detail_go(self, _b):
            card = self.cards.get(self.detail_pid)
            self.hide_detail()
            if card:
                self.on_card(self.flow, card)

        def toggle_drawer(self, show):
            if show:
                self.load_closed()
            self.split.set_show_sidebar(show)
            self.win.sync_drawer_btn(self)
            if show:
                self.search.grab_focus()

        def load_closed(self):
            live = {s["session_id"] for s in self.rows if s["session_id"]}
            while (row := self.closed_list.get_row_at_index(0)) is not None:
                self.closed_list.remove(row)
            for c in read_closed(self.cfg, live):
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
            subprocess.Popen(["ptyxis", "--new-window", "-d", c["cwd"], "--"] + claude_session_cmd(self.cfg, "--resume", c["session_id"]))
            self.toast(f"retomando «{c['title'][:60]}» em janela nova")
            self.toggle_drawer(False)

        def on_card(self, _flow, child):
            ok, msg = focus_session(child.session)
            if msg:
                self.toast(msg)

    class Win(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Claude Dash", default_width=760, default_height=520)
            self.wtitle = Adw.WindowTitle(title="Claude Dash")
            header = Adw.HeaderBar(title_widget=self.wtitle)
            new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Nova sessão na conta da aba atual (janela do Ptyxis com claude)")
            new_btn.connect("clicked", self.on_new)
            header.pack_start(new_btn)
            self.drawer_btn = Gtk.ToggleButton(icon_name="document-open-recent-symbolic",
                                               tooltip_text="Sessões encerradas da conta (retomar)")
            self.drawer_btn.connect("toggled", self.on_drawer_btn)
            header.pack_end(self.drawer_btn)
            self.set_icon_name("dev.araripe.ClaudeDash")
            # uma aba por conta: botões ligados (rótulo com contagem + ponto vermelho) sobre uma Gtk.Stack
            self.pages, self.tabs = {}, {}
            self.pstack = Gtk.Stack(hexpand=True, vexpand=True)
            tabbar = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=2)
            first_btn = None
            for name, cfg in ACCOUNTS:
                page = AccountPage(self, name, cfg)
                self.pages[name] = page
                self.pstack.add_named(page, name)
                label = Gtk.Label(label=name)
                dot = Gtk.Label(label="●", css_classes=["tab-dot"], visible=False,
                                tooltip_text="Sessão esperando você nesta conta")
                btn = Gtk.ToggleButton()
                inner = Gtk.Box(spacing=6)
                inner.append(label)
                inner.append(dot)
                btn.set_child(inner)
                btn.add_css_class("account-tab")
                if first_btn is None:
                    first_btn = btn
                else:
                    btn.set_group(first_btn)
                btn.connect("toggled", self.on_tab, name)
                tabbar.append(btn)
                self.tabs[name] = (btn, label, dot)
            self.tabbar = tabbar
            self.main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.main.append(tabbar)
            self.main.append(self.pstack)
            view = Adw.ToolbarView(content=self.main)
            view.add_top_bar(header)
            self.set_content(view)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.on_key)
            self.add_controller(keys)
            self.current = ACCOUNTS[0][0]
            if SHOT and "--account" in sys.argv:
                want = sys.argv[sys.argv.index("--account") + 1]
                self.current = want if want in self.pages else self.current
            self.tabs[self.current][0].set_active(True)
            self.pstack.set_visible_child_name(self.current)
            self.refresh()
            GLib.timeout_add(1500, self.refresh)
            if SHOT:
                if "--open" in sys.argv:
                    GLib.timeout_add(600, lambda: (self.open_first(), False)[1])
                if "--drawer" in sys.argv:
                    GLib.timeout_add(600, lambda: (self.page.toggle_drawer(True), False)[1])
                GLib.timeout_add(2500, self.shot)
            self.mons = []
            for _name, cfg in ACCOUNTS:  # status muda -> atualiza na hora (pasta que não existe: o timer cobre)
                try:
                    mon = Gio.File.new_for_path(sess_dir(cfg)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                    mon.connect("changed", lambda *_: self.refresh())
                    self.mons.append(mon)
                except Exception:
                    pass

        @property
        def page(self):
            return self.pages[self.current]

        def open_first(self):
            child = self.page.flow.get_child_at_index(0)
            if child:
                self.page.show_message(child.session)

        def on_tab(self, btn, name):
            if not btn.get_active():
                return
            self.current = name
            self.pstack.set_visible_child_name(name)
            self.sync_drawer_btn(self.page)
            self.update_tabs()

        def on_drawer_btn(self, btn):
            if btn.get_active() != self.page.split.get_show_sidebar():
                self.page.toggle_drawer(btn.get_active())

        def sync_drawer_btn(self, page):
            if page is self.page and self.drawer_btn.get_active() != page.split.get_show_sidebar():
                self.drawer_btn.set_active(page.split.get_show_sidebar())

        def update_tabs(self):
            """Rótulo com a contagem; ponto vermelho na aba que não está aberta e tem sessão esperando."""
            for name, page in self.pages.items():
                _btn, label, dot = self.tabs[name]
                label.set_text(f"{name} ({len(page.rows)})")
                dot.set_visible(name != self.current and any(s["status"] == "waiting" for s in page.rows))

        def refresh(self):
            n = {}
            for name, _cfg, rows in read_accounts():
                self.pages[name].refresh(rows)
                for s in rows:
                    key = "parked" if s["parked"] else s["status"]
                    n[key] = n.get(key, 0) + 1
            self.update_tabs()
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
            return True

        def shot(self):
            """Renderiza o conteúdo direto do nó de cena (não depende de frame do compositor)."""
            from gi.repository import Graphene
            root = self.main
            w, h = root.get_width(), root.get_height()
            if w == 0 or h == 0:
                return True
            snap = Gtk.Snapshot()
            snap.append_color(Gdk.RGBA(red=0.14, green=0.14, blue=0.14, alpha=1), Graphene.Rect().init(0, 0, w, h))

            def paint(widget):
                parent = widget.get_parent()
                ok, b = parent.compute_bounds(root)  # snapshot_child já aplica a posição do filho no pai
                if not ok:
                    return
                snap.save()
                snap.translate(Graphene.Point().init(b.origin.x, b.origin.y))
                parent.snapshot_child(widget, snap)
                snap.restore()
            page = self.page
            paint(self.tabbar)
            paint(page.stack)
            if page.dim.get_visible():
                paint(page.dim)
            if page.split.get_show_sidebar():
                paint(page.drawer)
            node = snap.to_node()
            if node is None:
                return True
            tex = self.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(SHOT)
            self.get_application().quit()
            return False

        def on_new(self, _btn):
            subprocess.Popen(["ptyxis", "--new-window", "-d", HOME, "--"] + claude_session_cmd(self.page.cfg))

        def switch_tab(self, step):
            names = [n for n, _c in ACCOUNTS]
            self.tabs[names[(names.index(self.current) + step) % len(names)]][0].set_active(True)

        def on_key(self, _c, keyval, _code, _state):
            page = self.page
            if page.search.has_focus():
                if keyval == Gdk.KEY_Escape:
                    page.toggle_drawer(False)
                    return True
                return False
            if keyval == Gdk.KEY_e:
                page.toggle_drawer(not page.split.get_show_sidebar())
                return True
            if keyval == Gdk.KEY_Escape and page.split.get_show_sidebar():
                page.toggle_drawer(False)
                return True
            if keyval in (Gdk.KEY_bracketleft, Gdk.KEY_bracketright):
                self.switch_tab(-1 if keyval == Gdk.KEY_bracketleft else 1)
                return True
            if Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
                child = page.flow.get_child_at_index(keyval - Gdk.KEY_1)
                if child:
                    page.on_card(page.flow, child)
                return True
            if keyval == Gdk.KEY_n:
                self.on_new(None)
                return True
            if keyval == Gdk.KEY_Escape and page.dim.get_visible():
                page.hide_detail()
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
