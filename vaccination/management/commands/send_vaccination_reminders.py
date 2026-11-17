from datetime import timedelta
from zoneinfo import ZoneInfo
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from vaccination.models import VaccineDose, SMSLog
from vaccination.sms import send_dose_reminder, normalize_ph_number


class Command(BaseCommand):
    help = "Send scheduled and missed-dose vaccination reminders via Semaphore."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Don't actually send, just simulate")
        parser.add_argument("--force", action="store_true", help="Force advance/day reminders only; never bypass missed-event protection")
        parser.add_argument("--retry-failed", action="store_true", help="Explicitly retry definite missed-message failures: max 3 attempts, 1 hour apart, within the seven-day recovery window")
        parser.add_argument("--approve-missed-backlog", action="store_true", help="Allow more than 25 recent catch-up reminders after administrator dry-run review; never expands the seven-day window")
        parser.add_argument("--type", choices=["3days", "on_day", "missed", "all"], default="all", help="Which reminder type to send")
        parser.add_argument("--limit", type=int, default=0, help="Limit number of messages (0 = no limit)")
        parser.add_argument("--date", type=str, default="", help="Override today as YYYY-MM-DD (for testing)")
        parser.add_argument("--verbose", action="store_true", help="Verbose output")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        force = options["force"]
        reminder_filter = options["type"]
        limit = options["limit"]
        verbose = options["verbose"]

        if options["date"]:
            from datetime import datetime as dt
            try:
                today = dt.strptime(options["date"], "%Y-%m-%d").date()
            except ValueError:
                self.stderr.write(self.style.ERROR(f"Invalid --date {options['date']}, use YYYY-MM-DD"))
                return
        else:
            today = timezone.localdate(timezone=ZoneInfo('Asia/Manila'))

        target_3days = today + timedelta(days=3)
        target_today = today

        self.stdout.write(self.style.NOTICE(f"Today: {today} | dry_run={dry_run} force={force} type={reminder_filter}"))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no SMS will be sent"))

        sent = 0
        skipped = 0
        failed = 0
        total_candidates = 0
        review_required = False

        # Helper to process a queryset
        def process_doses(qs, rtype):
            nonlocal sent, skipped, failed, total_candidates
            for dose in qs.select_related("schedule__bite_case__patient", "schedule__bite_case", "schedule__bite_case__patient__account"):
                total_candidates += 1
                if limit and (sent + failed + skipped) >= limit:
                    break
                # Try primary, then emergency, then account mobile (fallback)
                patient = dose.schedule.bite_case.patient
                candidates = [
                    patient.contact_number,
                    getattr(patient, "emergency_contact_number", ""),
                    getattr(getattr(patient, "account", None), "mobile", "") if getattr(patient, "account", None) else "",
                ]
                number = None
                normalized = None
                for cand in candidates:
                    n = normalize_ph_number(cand)
                    if n:
                        number = cand
                        normalized = n
                        break
                if not normalized:
                    if verbose:
                        self.stdout.write(self.style.WARNING(f"SKIP invalid number {candidates} for {dose}"))
                    skipped += 1
                    continue

                # Skip if already sent today (unless force)
                if not force and not dry_run:
                    # Check SMSLog for same dose + reminder_type + scheduled_date where sent today
                    already = SMSLog.objects.filter(
                        dose=dose,
                        reminder_type="3days_before" if rtype == "3days" else "on_day",
                        scheduled_date=dose.scheduled_date,
                        sent_at__date=today,
                        status__in=["sent", "queued", "pending"],
                    ).exists()
                    if already:
                        if verbose:
                            self.stdout.write(f"SKIP already sent today: {dose} ({rtype})")
                        skipped += 1
                        continue

                    # Also check general duplicate: if already sent for this reminder_type ever and scheduled_date same, avoid spamming
                    # But allow re-send next day? So we already guard by sent_at__date=today.

                rtype_mapped = "3days_before" if rtype == "3days" else "on_day"
                try:
                    if dry_run:
                        from vaccination.sms import build_reminder_message
                        msg = build_reminder_message(dose.schedule.bite_case.patient, dose.schedule.bite_case, dose, rtype_mapped)
                        self.stdout.write(self.style.SUCCESS(f"[DRY RUN] Would send {rtype} to {normalized} for {dose}: {msg[:90]}..."))
                        sent += 1
                    else:
                        log, result = send_dose_reminder(dose, reminder_type=rtype_mapped, dry_run=False, force=force)
                        if result.get("skipped"):
                            skipped += 1
                            self.stdout.write(self.style.WARNING(f"Skipped {dose}: {result.get('error')}"))
                        elif result.get("success"):
                            sent += 1
                            self.stdout.write(self.style.SUCCESS(f"Sent {rtype} to {normalized} for {dose} ({dose.scheduled_date}) - {log.status}"))
                            if verbose:
                                self.stdout.write(f"  -> {log.message[:100]}")
                        else:
                            failed += 1
                            self.stdout.write(self.style.ERROR(f"Failed {dose}: {result.get('error')}"))
                except Exception as e:
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"Exception for {dose}: {e}"))
                    if verbose:
                        import traceback
                        traceback.print_exc()

        # Query candidates
        base_filter = Q(dose_status__in=["scheduled", "rescheduled"])
        # Also ensure patient has contact and not archived? Patient model has is_archived

        if reminder_filter in ("3days", "all"):
            qs_3 = VaccineDose.objects.filter(base_filter, scheduled_date=target_3days).select_related("schedule__bite_case__patient", "schedule__bite_case__patient__account")
            qs_3 = qs_3.filter(schedule__bite_case__patient__is_archived=False)
            count3 = qs_3.count()
            self.stdout.write(f"Found {count3} doses due 3 days before (scheduled_date={target_3days})")
            process_doses(qs_3, "3days")

        if reminder_filter in ("on_day", "all"):
            qs_today = VaccineDose.objects.filter(base_filter, scheduled_date=target_today).select_related("schedule__bite_case__patient", "schedule__bite_case__patient__account")
            qs_today = qs_today.filter(schedule__bite_case__patient__is_archived=False)
            count_t = qs_today.count()
            self.stdout.write(f"Found {count_t} doses due TODAY (scheduled_date={target_today})")
            process_doses(qs_today, "on_day")

        if reminder_filter in ("missed", "all"):
            from vaccination.missed_appointments import (
                MISSED_CATCH_UP_REVIEW_LIMIT, MISSED_REMINDER_LOOKBACK_DAYS,
                missed_reminder_message, missed_sms_consent_ok,
                missed_reminder_doses, send_missed_appointment_reminder,
            )

            missed_doses = missed_reminder_doses(today=today)
            yesterday = today - timedelta(days=1)
            catch_up_count = missed_doses.filter(scheduled_date__lt=yesterday).count()
            self.stdout.write(
                f"Found {missed_doses.count()} recent unadministered doses "
                f"({today - timedelta(days=MISSED_REMINDER_LOOKBACK_DAYS)} through {today}; "
                "today requires an explicit missed status); "
                f"catch-up candidates={catch_up_count}"
            )
            if catch_up_count > MISSED_CATCH_UP_REVIEW_LIMIT and not options['approve_missed_backlog']:
                self.stdout.write(self.style.WARNING(
                    f'{catch_up_count} catch-up reminders require administrator review. '
                    'Preview with --type missed --dry-run, then use --approve-missed-backlog '
                    'only after review. Yesterday\'s reminders are still processed normally.'
                ))
                if not dry_run:
                    review_required = True
                    skipped += catch_up_count
                    missed_doses = missed_doses.filter(scheduled_date=yesterday)
            for dose in missed_doses:
                total_candidates += 1
                if limit and (sent + failed + skipped) >= limit:
                    break
                patient = dose.schedule.bite_case.patient
                normalized = normalize_ph_number(patient.contact_number)
                if dry_run and not missed_sms_consent_ok(dose.schedule.bite_case):
                    skipped += 1
                    self.stdout.write(self.style.WARNING(
                        f"SKIP no SMS consent for {dose}"
                    ))
                    continue
                if dry_run:
                    if not normalized:
                        skipped += 1
                        self.stdout.write(self.style.WARNING(
                            f"SKIP invalid primary contact for {dose}"
                        ))
                        continue
                    self.stdout.write(self.style.SUCCESS(
                        f"[DRY RUN] Would send missed appointment reminder "
                        f"to {normalized} for {dose}: "
                        f"{missed_reminder_message(dose)}"
                    ))
                    sent += 1
                    continue
                try:
                    if options['retry_failed']:
                        # Staff-reviewed retries retain the existing explicit path.
                        log, result = send_missed_appointment_reminder(
                            dose.pk, today=today, retry_failed=True,
                            expected_scheduled_date=dose.scheduled_date,
                        )
                    else:
                        from notifications.utils import dispatch_missed_schedule
                        _, log, result = dispatch_missed_schedule(
                            dose.pk, today=today, expected_date=dose.scheduled_date,
                        )
                except Exception as exc:
                    failed += 1
                    self.stdout.write(self.style.ERROR(
                        f"Failed missed reminder for {dose}: {exc}"
                    ))
                    continue
                if result.get("skipped"):
                    skipped += 1
                    self.stdout.write(self.style.WARNING(
                        f"Skipped missed reminder for {dose}: "
                        f"{result.get('error')}"
                    ))
                elif log and log.status == 'queued':
                    skipped += 1
                    self.stdout.write(self.style.WARNING(
                        f"Queued without submission for dose {dose.pk}: SMS gateway disabled"
                    ))
                elif log and log.status == "sent":
                    sent += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"Missed appointment reminder {log.status} for "
                        f"{dose} to {log.recipient}"
                    ))
                else:
                    failed += 1
                    self.stdout.write(self.style.ERROR(
                        f"Failed missed reminder for {dose}: "
                        f"{result.get('error')}"
                    ))

        self.stdout.write(self.style.SUCCESS(f"Done. Candidates={total_candidates} Sent={sent} Skipped={skipped} Failed={failed}"))

        if failed or review_required:
            raise CommandError(
                f'{failed} reminder(s) failed or have an unknown outcome; '
                f'backlog review required={review_required}. Review SMS history before retrying.'
            )
