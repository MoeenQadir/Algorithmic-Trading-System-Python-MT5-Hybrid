#!/usr/bin/env python3
"""Development startup script for Trading Framework (Phase 1).

Usage:
    python scripts/dev.py [--port PORT] [--no-browser]

Starts both backend and frontend in development mode with hot reload.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"
PYTHON = VENV / "Scripts" / "python.exe" if os.name == "nt" else VENV / "bin" / "python"
LOGS = ROOT / "logs"


def find_free_port(start: int = 3000) -> int:
    for port in range(start, start + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("Could not find free port")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Trading Framework in development mode")
    parser.add_argument("--port", type=int, default=3000, help="Frontend port (default: auto-find from 3000)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    # Find free port
    frontend_port = find_free_port(args.port)
    if frontend_port != args.port:
        print(f"Port {args.port} in use, using {frontend_port}")

    # Ensure logs directory
    LOGS.mkdir(exist_ok=True)

    # Ensure .env exists
    env_file = ROOT / ".env"
    env_example = ROOT / ".env.example"
    if not env_file.exists() and env_example.exists():
        print("Creating .env from .env.example...")
        env_file.write_text(env_example.read_text())

    # Set PYTHONPATH
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND)

    print(f"=== Starting Trading Framework (dev mode) ===")
    print(f"Backend:  http://127.0.0.1:8000/docs")
    print(f"Frontend: http://127.0.0.1:{frontend_port}/dashboard")
    print(f"Logs:     {LOGS}")
    print("Press Ctrl+C to stop\n")

    processes = []

    try:
        # Start backend with uvicorn --reload
        backend_proc = subprocess.Popen(
            [str(PYTHON), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--reload"],
            cwd=ROOT,
            env=env,
            stdout=open(LOGS / "backend-stdout.log", "a"),
            stderr=open(LOGS / "backend-stderr.log", "a"),
        )
        processes.append(("backend", backend_proc))
        print(f"Backend started (PID: {backend_proc.pid})")

        # Give backend time to start
        time.sleep(2)

        # Start frontend with npm run dev
        frontend_proc = subprocess.Popen(
            ["npm", "run", "dev", "--", "--port", str(frontend_port)],
            cwd=FRONTEND,
            env=env,
            stdout=open(LOGS / "frontend-stdout.log", "a"),
            stderr=open(LOGS / "frontend-stderr.log", "a"),
        )
        processes.append(("frontend", frontend_proc))
        print(f"Frontend started (PID: {frontend_proc.pid})")

        # Open browser
        if not args.no_browser:
            time.sleep(3)
            url = f"http://127.0.0.1:{frontend_port}/dashboard"
            if sys.platform == "win32":
                os.startfile(url)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])

        # Wait for processes
        while True:
            for name, proc in processes:
                if proc.poll() is not None:
                    print(f"\n{name} process exited with code {proc.returncode}")
                    return proc.returncode
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for name, proc in processes:
            if proc.poll() is None:
                print(f"Stopping {name} (PID: {proc.pid})...")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    return 0


if __name__ == "__main__":
    sys.exit(main())