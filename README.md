# BBRadar Monitor

Monitor 24/7 de novos programas em [bbradar.io](https://bbradar.io) via GitHub Actions.

## Como funciona

- Roda automaticamente a cada **15 minutos** via cron do GitHub Actions
- Na **primeira execução**, cataloga todos os programas existentes (sem notificar)
- A partir da **segunda execução**, notifica via Discord, Telegram e WhatsApp apenas quando aparecer algo novo
- Estado persistido em `state.json` (commitado automaticamente)
- Página de status em `index.html` (GitHub Pages)

## ⚠️ Este repo DEVE ser privado

As credenciais estão hardcoded em `monitor.py`.

## Status

Veja `index.html` via GitHub Pages.
