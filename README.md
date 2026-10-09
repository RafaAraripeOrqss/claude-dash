# claude_dash

Painel GTK para acompanhar todas as sessões do Claude Code da máquina, abertas com `claude`
normal. Card **verde** = disponível, **amarelo** = trabalhando, **vermelho** = esperando você
(permissão ou pergunta), **azul** = no shell (status `shell` do próprio Claude: sessão parada num
comando de shell; ativa, não inativa). Grade de dois cards por linha; cada card traz o
título da sessão e o começo da última mensagem do Claude; o chevron no canto abre a tela de
detalhe do card (mesma cor do status, mensagem inteira selecionável, botão "Ir para a janela";
fecha com clique fora ou Esc). Clique no card (ou tecla
1-9) foca a janela do Ptyxis daquela sessão. Botão **+** (ou `n`) abre uma sessão nova numa janela do Ptyxis.
O botão de histórico (ou `e`) abre o drawer de **sessões encerradas**: transcripts sem processo
vivo, mais recentes primeiro (até 80), com filtro por título; clique retoma a sessão numa janela
nova do Ptyxis (`claude --resume <id>` na pasta original). Sessões de um comando só não aparecem.

```bash
claude-dash                 # janela GTK (também no menu do GNOME: "Claude Dash", ícone Cⁿ)
claude-dash --text          # tabela no terminal
claude-dash --shot x.png    # renderiza a janela e sai (conferência); --open abre o detalhe do 1º card, --drawer o drawer
```

## Como funciona

- **Status** vem de `~/.claude/sessions/<pid>.json`, que o próprio Claude Code (≥ 2.1.x) grava:
  `status` ∈ {`idle`, `busy`, `waiting`, `shell`}, `name`, `cwd`, `sessionId`. Sem hook.
  Sessão morta é descartada pelo `/proc/<pid>/stat` (pid + starttime).
- **Título e última mensagem** vêm do fim do transcript `~/.claude/projects/<cwd>/<sessionId>.jsonl`:
  último `custom-title` (`/rename`) ou `ai-title`, e o último bloco de texto de uma linha
  `assistant` da conversa principal (subagentes, `isSidechain`, ficam de fora). Lido só quando o
  arquivo muda (cache por mtime/tamanho), sem hook.
- **Foco** usa a extensão GNOME *Activate Window By Title* via D-Bus
  (`de.lucaswerkmeister.ActivateWindowByTitle.activateBySubstring`), casando com o título que o
  Claude escreve na janela (`✳ <título> — claude`; o glifo muda com o status). Funciona com
  **uma sessão por janela** do Ptyxis, que é o uso normal. O Ptyxis só foca aba por um uuid
  interno que não sai do processo: se as sessões estiverem em abas da mesma janela, o clique
  foca a janela e avisa. (AT-SPI expõe a árvore do Ptyxis, mas sem as abas; fica como caminho
  se um dia precisar.)
- Sessão em **background** (`/bg`, `←`): vive no daemon do Claude, então **fechar o terminal não
  a encerra** e ela continua no painel, marcada "background · sem terminal" (ou "terminal anexado"
  quando há um `claude attach` vivo). Clique tenta a janela pelo nome e, se não houver, abre
  `claude attach <nome>` numa janela nova. A tela de detalhe tem o botão "Encerrar sessão"
  (`claude stop <id>`; a conversa fica salva). O host "estacionado" e a sessão `spare` do Agent
  View ficam escondidos.

## Requisitos

- GNOME em Wayland (testado no GNOME 50 / Ubuntu) e o terminal **Ptyxis**, uma sessão por janela.
- Python 3 com `python3-gi`, GTK 4 e libadwaita (`apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`).
- Claude Code 2.1.x ou mais novo (é ele que grava `~/.claude/sessions/`).
- Extensão GNOME *Activate Window By Title* (o `install.sh` instala pelo próprio Shell).

## Instalação

```bash
git clone https://github.com/RafaAraripeOrqss/claude-dash.git ~/Scripts/claude_dash
~/Scripts/claude_dash/install.sh
```

O script cria `~/.local/bin/claude-dash`, o ícone e a entrada "Claude Dash" no menu, e instala/ativa a
extensão sem precisar de logout. Rode de novo para atualizar depois de um `git pull`.

## Verificação

```bash
claude-dash --text
gdbus call --session --dest org.gnome.Shell --object-path /de/lucaswerkmeister/ActivateWindowByTitle \
  --method de.lucaswerkmeister.ActivateWindowByTitle.activateBySubstring "parte do título"
```

## Sync automático (dono do repo)

`sync/claude-dash-sync.path` (systemd --user) vigia a pasta; 20 s depois de qualquer mudança,
`sync/sync.sh` commita (`sync: <data>`) e faz push na main. Ativar:

```bash
ln -sfn ~/Scripts/claude_dash/sync/claude-dash-sync.{service,path} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now claude-dash-sync.path
```
