"""Supervise web and SMS scheduler together so they share local SQLite."""
import os
import signal
import subprocess
import sys
import threading


def main():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    commands = [[sys.executable, "-m", "gunicorn", "config.wsgi:application",
                 "--bind", "0.0.0.0:80"]]
    if os.environ.get("MISSED_SMS_SCHEDULER_ENABLED", "True").lower() in ("true", "1", "yes"):
        commands.append([sys.executable, "manage.py", "run_missed_reminder_scheduler"])
    children = []
    exit_code = 0
    try:
        for command in commands:
            children.append(subprocess.Popen(command))
        while not stop.wait(1):
            if any(child.poll() is not None for child in children):
                exit_code = 1
                break
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
