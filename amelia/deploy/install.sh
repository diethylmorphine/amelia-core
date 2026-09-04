#!/usr/bin/env bash
# Установка Амелии как круглосуточной службы на Debian/Ubuntu.
# Рассчитано на б/у микро-ПК: Dell OptiPlex Micro, Lenovo ThinkCentre Tiny, HP EliteDesk Mini.
#
#   sudo bash deploy/install.sh
#
set -euo pipefail

APP_DIR=/opt/amelia
APP_USER=amelia
MODEL="${AMELIA_MODEL:-qwen2.5:3b}"
EMBED="${AMELIA_EMBED:-bge-m3}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Нужен root: sudo bash $0"; exit 1; }

say "Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates tzdata

say "Пользователь службы"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

say "Код в $APP_DIR"
mkdir -p "$APP_DIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SRC" != "$APP_DIR" ]]; then
  cp -r "$SRC/amelia" "$SRC/pyproject.toml" "$SRC/README.md" "$APP_DIR/"
  [[ -f "$APP_DIR/.env" ]] || cp "$SRC/.env.example" "$APP_DIR/.env"
fi
mkdir -p "$APP_DIR"/{data,vault,data/backups}

say "Виртуальное окружение"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[telegram]"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "Ollama"
if ! command -v ollama &>/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
mkdir -p /etc/systemd/system/ollama.service.d
cp "$SRC/deploy/ollama-override.conf" /etc/systemd/system/ollama.service.d/override.conf
systemctl daemon-reload
systemctl enable --now ollama
sleep 3

say "Модели: $MODEL и $EMBED"
echo "Займёт несколько минут на первом запуске."
ollama pull "$MODEL"
ollama pull "$EMBED"

say "Служба"
cp "$SRC/deploy/amelia.service" /etc/systemd/system/amelia.service
cp "$SRC/deploy/logrotate.amelia" /etc/logrotate.d/amelia
sed -i "s|^AMELIA_LLM_MODEL=.*|AMELIA_LLM_MODEL=$MODEL|" "$APP_DIR/.env"
sed -i "s|^AMELIA_DB_PATH=.*|AMELIA_DB_PATH=$APP_DIR/data/amelia.db|" "$APP_DIR/.env"
sed -i "s|^AMELIA_VAULT_PATH=.*|AMELIA_VAULT_PATH=$APP_DIR/vault|" "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
systemctl daemon-reload
systemctl enable --now amelia

say "Автовключение после сбоя питания"
# Микро-ПК после пропадания света должен подниматься сам — это в BIOS,
# пункт «AC Power Recovery» / «After Power Loss» -> Power On. Скриптом не задать.

say "Готово"
sleep 2
systemctl --no-pager --lines=5 status amelia || true
cat <<'TXT'

Дальше руками:
  1. Токен бота:   sudo -e /opt/amelia/.env   -> AMELIA_BOT_TOKEN, AMELIA_OWNER_ID
     затем         sudo systemctl restart amelia
  2. Проверка:     curl -s localhost:8787/health | python3 -m json.tool
  3. Логи:         journalctl -u amelia -f
  4. В BIOS включи «After Power Loss -> Power On», иначе после отключения
     света коробка не встанет сама.
TXT
