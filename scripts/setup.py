#!/usr/bin/env python3
"""One-command setup script for Trading Framework (Phase 1).

Usage:
    python scripts/setup.py

This script:
1. Creates .venv if missing
2. Installs Python dependencies
3. Installs Node dependencies
4. Runs alembic migration
5. Creates .env from .env.example if missing
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"
PYTHON = VENV / "Scripts" / "python.exe" if os.name == "nt" else VENV / "bin" / "python"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


def main() -> int:
    print("=== Trading Framework Setup ===\n")

    # 1. Create virtual environment
    if not VENV.exists():
        print("Creating Python virtual environment...")
        run([sys.executable, "-m", "venv", ".venv"])
    else:
        print("Virtual environment already exists.")

    # 2. Upgrade pip and install Python deps
    print("\nInstalling Python dependencies...")
    run([str(PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(PYTHON), "-m", "pip", "install", "-r", str(BACKEND / "requirements.txt")])

    # 3. Install Node dependencies
    print("\nInstalling Node dependencies...")
    run(["npm", "install"], cwd=FRONTEND)

    # 4. Create .env from .env.example if missing
    env_file = ROOT / ".env"
    env_example = ROOT / ".env.example"
    if not env_file.exists() and env_example.exists():
        print("\nCreating .env from .env.example...")
        env_file.write_text(env_example.read_text())
        print("Created .env - review and edit as needed.")
    elif env_file.exists():
        print("\n.env already exists - skipping creation.")

    # 5. Run alembic migration
    print("\nRunning database migrations...")
    run([str(PYTHON), "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"])

    print("\n=== Setup Complete ===")
    print("\nNext steps:")
    print("  1. Edit .env if needed (e.g., add OPENROUTER_API_KEY for LLM features)")
    print("  2. Start the application: python scripts/dev.py")
    print("     Or use: start.ps1 (Windows) / start.bat (Windows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())