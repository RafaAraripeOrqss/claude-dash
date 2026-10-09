#!/usr/bin/env bash
# Instala o claude_dash para o usuário atual: atalho, ícone, entrada no menu e a extensão GNOME
# que permite focar a janela do Ptyxis. Não usa sudo. Rode de novo para atualizar.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="dev.araripe.ClaudeDash"
EXT="activate-window-by-title@lucaswerkmeister.de"

ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

echo "Dependências"
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' 2>/dev/null \
  && ok "python3-gi com GTK 4 e libadwaita" \
  || { warn "faltam python3-gi / gir1.2-gtk-4.0 / gir1.2-adw-1 (apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1)"; }
command -v ptyxis >/dev/null && ok "ptyxis" || warn "ptyxis não encontrado: o foco e o botão + dependem dele"
command -v claude >/dev/null && ok "claude $(claude --version 2>/dev/null | head -1)" || warn "claude não está no PATH"
[ "${XDG_SESSION_TYPE:-}" = "wayland" ] && [ "${XDG_CURRENT_DESKTOP:-}" != "${XDG_CURRENT_DESKTOP/GNOME/}" ] \
  && ok "GNOME Wayland" || warn "feito para GNOME Wayland; em outro ambiente o foco da janela não funciona"

echo "Arquivos"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sfn "$HERE/claude_dash.py" ~/.local/bin/claude-dash && ok "~/.local/bin/claude-dash"
cp "$HERE/data/$APP_ID.svg" ~/.local/share/icons/hicolor/scalable/apps/ && ok "ícone"
cat > ~/.local/share/applications/$APP_ID.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Claude Dash
Comment=Sessões do Claude Code: verde livre, amarelo trabalhando, vermelho esperando você
Exec=$HERE/claude_dash.py
Icon=$APP_ID
Terminal=false
Categories=Development;
StartupNotify=true
DESK
ok "entrada no menu (Claude Dash)"
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q ~/.local/share/icons/hicolor 2>/dev/null || true

echo "Extensão GNOME (Activate Window By Title)"
if gnome-extensions info "$EXT" >/dev/null 2>&1; then
  gnome-extensions enable "$EXT" 2>/dev/null || true
  ok "já instalada ($(gnome-extensions info "$EXT" | grep -iE 'estado|state' | tr -s ' '))"
else
  # pedir ao próprio Shell que instale: carrega na hora, sem logout (abre um diálogo de confirmação)
  if gdbus call --session --dest org.gnome.Shell.Extensions --object-path /org/gnome/Shell/Extensions \
       --method org.gnome.Shell.Extensions.InstallRemoteExtension "$EXT" >/dev/null 2>&1; then
    ok "instalada pelo Shell"
  else
    warn "não consegui instalar pelo Shell; instale em https://extensions.gnome.org/extension/5021/ e rode de novo"
  fi
  sleep 1; gnome-extensions enable "$EXT" 2>/dev/null || true
fi

echo "Pronto: abra 'Claude Dash' no menu ou rode claude-dash."
