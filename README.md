# Amelia Core

Amelia is a local-first Python assistant with a modular request pipeline, an Ollama-backed language model, SQLite memory and reminders, and CLI or Telegram clients.

## Architecture

- `amelia/amelia/core/`: request kernel, routing, modules, memory, scheduler, agent, tracing, and generated-skill tooling.
- `amelia/amelia/modules/`: built-in chat, notes, recall, reminders, system, upkeep, actions, and forge modules.
- `amelia/amelia/clients/`: command-line, service, and optional Telegram clients.
- `amelia/tests/`: regression and module tests.

The detailed Russian guide covers configuration, deployment, current behavior, and known limitations: [amelia/README.md](amelia/README.md).

## Quick start

Python 3.11 or newer is required. From the repository root:

```sh
cd amelia
python -m venv .venv
# Activate the virtual environment, then:
python -m pip install -e ".[dev]"
python -m amelia cli
```

The CLI can run without a model service; free-form LLM responses require Ollama. The Telegram client is optional and requires the `telegram` extra, `AMELIA_BOT_TOKEN`, and `AMELIA_OWNER_ID`; it will not start without an owner ID. See the detailed guide for service setup and configuration.

## Generated-skill security warning

The forge's AST validator is a static filter, not a security boundary. Generated code is not executed until you explicitly approve it; approval runs a first-import probe in a subprocess with a 20-second timeout. That subprocess uses the same OS account and is not isolated by a container, sandbox, filesystem restrictions, or resource limits. Once enabled, a generated skill executes in the Amelia process with its restricted `SkillKernel` facade. The facade is also bypassable through ordinary Python private attributes such as `memory._memory`; Python code must not be treated as contained or safe. Review generated code before enabling it. `AMELIA_FORGE_AUTO_APPROVE` is retained for configuration compatibility but no longer bypasses explicit approval.

## Checks

From `amelia/` with the development extra installed:

```sh
python -m pytest
```
