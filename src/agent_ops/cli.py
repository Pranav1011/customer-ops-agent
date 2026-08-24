"""Tiny CLI entrypoint (placeholder; real subcommands land with later phases)."""

from __future__ import annotations

from agent_ops import __version__
from agent_ops.config import get_settings


def main() -> None:
    s = get_settings()
    print(f"agent-ops {__version__}")
    print(f"llm_provider={s.llm_provider} db={s.db_file}")


if __name__ == "__main__":
    main()
