#!/usr/bin/env bash
# Быстрый старт для разработки
set -e
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp -n .env.example .env || true
echo "Готово. Запуск: python -m amelia cli"
