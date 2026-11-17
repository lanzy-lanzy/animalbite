"""One daily missed-dose check; uses the same database as the web process."""
import os
from datetime import time
from io import StringIO
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone


def run_due_check(state_path, now=None):
    local_now = (now or timezone.now()).astimezone(ZoneInfo("Asia/Manila"))
    if local_now.time() < time(9):
        return "waiting"
    if not settings.SEMAPHORE_ENABLED or not settings.SEMAPHORE_API_KEY:
        return "gateway_disabled"
    today = local_now.date().isoformat()
    if state_path.exists() and state_path.read_text().strip() == today:
        return "already_checked"
    close_old_connections()
    try:
        # The existing command enforces consent, seven-day recovery, backlog
        # review and durable duplicate protection. Never enable force/retries.
        # Its per-patient output must not enter public infrastructure logs.
        call_command("send_vaccination_reminders", type="missed",
                     stdout=StringIO(), stderr=StringIO())
    finally:
        close_old_connections()
        # One attempt per day, including partial failures. Clinical staff review
        # failed/unknown SMS outcomes; the scheduler never retries them itself.
        pending = state_path.with_suffix(".tmp")
        pending.write_text(today)
        os.replace(pending, state_path)
    return "checked"
