#!/usr/bin/env python3
"""Create a portable Python zipapp from the monitor source (stdlib only)."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the portable DGSM .pyz application")
    parser.add_argument("--output", type=Path, default=Path("dist/linux/DiscordGlobalStatusMonitor.pyz"))
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    source = root / "discord_server_global_status.py"
    if not source.is_file():
        parser.error(f"Monitor source not found: {source}")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dgsm-zipapp-") as temp_dir:
        build_dir = Path(temp_dir)
        shutil.copy2(source, build_dir / source.name)
        (build_dir / "__main__.py").write_text(
            "from discord_server_global_status import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        zipapp.create_archive(
            build_dir,
            target=output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    output.chmod(0o755)
    print(f"Created Python zipapp: {output} ({output.stat().st_size} bytes)")
    print("Run with: python3 " + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
