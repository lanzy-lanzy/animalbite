import signal
import threading
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from vaccination.reminder_scheduler import run_due_check


class Command(BaseCommand):
    help = "Run the daily 09:00 Asia/Manila missed-dose SMS check in this service."

    def handle(self, *args, **options):
        # Seenode is Linux. One lock covers all scheduler processes in this
        # container; SMS event claims protect against overlapping senders too.
        import fcntl

        state = Path(settings.BASE_DIR) / ".missed_sms_scheduler.state"
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with state.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError("A missed-dose scheduler is already running.") from None
            self.stdout.write("Missed-dose scheduler started: 09:00 Asia/Manila.")
            self.stdout.flush()
            while not stop.is_set():
                try:
                    from notifications.utils import dispatch_marked_missed_schedules
                    # Explicit missed status changes must not wait until tomorrow
                    # or be blocked by the daily run's already-checked marker.
                    dispatch_marked_missed_schedules()
                    result = run_due_check(state)
                    if result == "checked":
                        self.stdout.write("Daily missed-dose check completed; review SMS history for outcomes.")
                        self.stdout.flush()
                except Exception:
                    self.stderr.write("Daily missed-dose check requires review; inspect SMS history. No automatic failure retry.")
                    self.stderr.flush()
                stop.wait(60)
