# Animal Bite Project

Django application for managing animal bite cases.

## Frontend (Tailwind CSS v4)

```bash
cd static
npm run build   # one-time build
npm run watch   # watch mode
```

## Vaccination Schedule — Patient Management

Sidebar → **Patient Management → Vaccination Schedule** (`/vaccination/schedules/patients/`)
- Search by patient name / PAT-xxx / case / contact
- Filter by barangay / dose status / due (Today / Overdue / Upcoming)
- See next due dose, progress, overdue badge, tap **Manage** to update doses
- Also from `Patients → Schedules` pill or `Patient Detail → Vaccination` column

## SMS Reminders via Semaphore (RHUDumingag)

Automatic SMS **3 days before**, **on the appointment day**, and **the morning after a missed appointment**, plus manual per-dose send and bounded recovery after scheduler downtime.

- **Sender Name:** `RHUDumingag` (approve it at https://semaphore.co/account → Sender Names)
- **API docs:** https://semaphore.co/docs → `POST https://api.semaphore.co/api/v4/messages` with `apikey, number, message, sendername`
- **Number format:** `09XXXXXXXXX` / `639XXXXXXXXX` auto-normalized to `639XXXXXXXXX`

### 1. Configure `.env`

Copy `.env.example` → `.env` and fill:

```
SEMAPHORE_API_KEY=your_key_from_semaphore.co
SEMAPHORE_SENDER_NAME=RHUDumingag
SEMAPHORE_ENABLED=False   # True to actually send (costs credits). False = dry-run / simulated
```

> When `SEMAPHORE_ENABLED=False`, messages are **not sent** but logged as `queued (simulated)` — perfect for testing without spending credits.

### 2. Test manually

```bash
# Dry-run (no credits, shows what would send)
python manage.py send_vaccination_reminders --dry-run --verbose

# Test for a past/future date (e.g., doses scheduled on 2026-05-06)
python manage.py send_vaccination_reminders --date 2026-05-06 --dry-run --verbose

# Actually send (needs valid SEMAPHORE_API_KEY + SEMAPHORE_ENABLED=True)
python manage.py send_vaccination_reminders --verbose

# Force re-send even if already sent today
python manage.py send_vaccination_reminders --force --verbose

# Only 3-days or only on-day
python manage.py send_vaccination_reminders --type 3days --verbose
python manage.py send_vaccination_reminders --type on_day --verbose
python manage.py send_vaccination_reminders --type missed --verbose
```

The daily command sends an **Automatic Missed Appointment Reminder** the morning
after the appointment in **Asia/Manila**. It also recovers unprocessed appointments
from the **last seven calendar days** after scheduler downtime. Only current saved
appointments on active (new/under-treatment/missed) cases with non-archived patients
qualify. Completed or cancelled doses, any recorded actual administration date or
time, today/future dates, undated doses and dates older than seven days are excluded.
A rescheduled dose is evaluated using its new date, never its superseded appointment.

If more than **25 catch-up candidates** are older than yesterday, the command holds
that catch-up batch for administrator review, reports a nonzero exit code, and still
processes yesterday's reminders and the normal advance/day reminders. Preview the
batch with `--type missed --dry-run`; after reviewing eligibility and saved history,
an administrator can explicitly use `--type missed --approve-missed-backlog` (with
`--limit N` if needed). Approval never expands the seven-day window. Do not add the
approval flag to the scheduled task or automatically process historical backlogs.

A pristine Pending SMSLog (zero attempts, no gateway reference or response) can be
claimed and processed in place. Sent and unknown-outcome events are never resent;
ambiguous legacy events require operator review rather than automatic retries.

Affirmative consent must exist in the linked pre-registration. Missing consent
(including walk-ins with no recorded consent), declined consent, and an invalid
primary contact produce a **Skipped** history record with the reason. Never
substitute emergency contacts or assume consent; staff must resolve missing
consent through the clinic's registration process before automatic texting.

The message uses the actual ordinal dose and missed date, without a name or case ID:

> RHU Dumingag ABTC: You missed your scheduled {dose_number} on {scheduled_date}.
> Please visit or contact RHU Dumingag as soon as possible to arrange your next
> vaccination appointment. Thank you.

`{dose_number}` is the actual ordinal label, such as `2nd Dose`.

No vaccination or administration records are modified. Each case/dose/date event
has a unique key in the existing SMSLog. A durable database claim is committed
**before** contacting Semaphore. Concurrent runs, `--force`, crashes, and timeouts
cannot automatically repeat a possibly accepted send. A newly rescheduled date
can produce its own event when missed, without overwriting earlier SMS history.

Statuses: **Queued** = not submitted (including disabled-gateway simulations);
**Sent** = gateway accepted with a reference, not proof of delivery;
**Failed** = definite rejection/configuration error; **Outcome Unknown** =
in-flight, timeout, malformed response or unconfirmed acceptance; **Skipped** =
not submitted because eligibility/contact/consent changed or is missing.
No delivery receipts are available, so the application never asserts Delivered.

There are **no automatic failure retries**. An administrator can explicitly run
`--type missed --retry-failed` after correcting a definite rejection.
Only failures marked retryable are eligible, with a one-hour cooldown, three
attempts maximum, and only within the seven-day recovery window. Unknown outcomes
and successful events never retry; verify unknown results with the provider.
`--date` is an explicit historical override: review using `--dry-run` first;
never add it, `--retry-failed`, `--approve-missed-backlog`, or `--force` to the
scheduled task. Appointments older than seven days require separate clinical and
administrator review; the daily job does not automatically text that backlog.

The **Send Due Reminders** button continues to send advance/day reminders.
Schedule-confirmation and manual per-dose SMS remain available. None of these
buttons, page visits, or the web server are needed for the missed reminder job.

### 3. Automate daily (Asia/Manila 09:00)

Reuse **one** scheduler for `send_vaccination_reminders --type all`, which covers
advance, appointment-day and missed reminders. Do not also schedule a separate
missed command. The computer, database service and internet must be available;
no Django development server is required. An offline computer cannot send SMS.

**Windows / this local deployment (PowerShell, Run as Administrator):**

1. Install dependencies in the project's `.venv`, ensure MariaDB/MySQL starts
   as a Windows service, and verify the database configured by the project's
   `.env`. Keep the API key in `.env`, never in task arguments.
2. Apply the SMS migrations to that same database (no fake migrations):

   ```powershell
   & 'C:\Users\Jasper\animalbite_project\.venv\Scripts\python.exe' 'C:\Users\Jasper\animalbite_project\manage.py' migrate vaccination
   ```

   Migration `0008` supplies the existing unique `event_key`; `0010` adds the
   Unknown/Skipped status choices and corrects old queued missed logs only when
   their saved response proves real gateway acceptance. No history is deleted.
3. Preview with **no sends or log writes**:

   ```powershell
   & 'C:\Users\Jasper\animalbite_project\.venv\Scripts\python.exe' 'C:\Users\Jasper\animalbite_project\manage.py' send_vaccination_reminders --type all --dry-run
   ```

4. Register/update the existing task (no immediate run):

   ```powershell
   & 'C:\Users\Jasper\animalbite_project\scripts\configure_sms_scheduler.ps1' -Time '09:00'
   Get-ScheduledTask -TaskName 'ABTC_SMS_Reminders'
   Get-ScheduledTaskInfo -TaskName 'ABTC_SMS_Reminders'
   ```

   `-Time` is administrator-configurable Philippine time, explicitly UTC+08:00.
   The script backs up the previous task XML in the current user's temp folder,
   runs Python directly with the project working directory, uses S4U to run when
   logged out without storing a password, skips overlapping runs, allows battery
   operation, wakes the computer if supported, and starts missed triggers when
   available. It requires an elevated shell to register the logged-out task.
   `Ready` only means the task is registered. `LastTaskResult=267011` (`0x41303`)
   means it has never run; confirm a real LastRunTime and a successful result after
   the next trigger. An Interactive-only task needs a logged-in user and is not an
   unattended setup. If registration reports Access Denied, use Run as Administrator;
   do not treat the existing task as upgraded. The script checks elevation before
   changing the task, and `-WhatIf` is a read-only preview.
   S4U cannot access Windows-authenticated network shares/EFS; if the installation
   depends on those, choose a dedicated account with stored credentials through
   Task Scheduler's **Run whether user is logged on or not** setting instead.
5. Enable `SEMAPHORE_ENABLED=True` only for real delivery with approved sender and
   credits. Do not use `Start-ScheduledTask` as a test on real patient data.
   Monitor NextRunTime/LastTaskResult and staff SMS history after the daily run.

**Linux cron alternative (use instead of Windows scheduling):**

Apply the same migrations and configure the same database and Semaphore secrets.
On a UTC-hosted cron server, 01:00 UTC is 09:00 Philippine time:

```cron
0 1 * * * /srv/animalbite_project/.venv/bin/python /srv/animalbite_project/manage.py send_vaccination_reminders --type all >> /var/log/abtc_sms.log 2>&1
```

Replace paths with the real deployment paths and protect the log for clinic staff
only. For cron running in Asia/Manila use `0 9 * * *` instead. Confirm the cron
service's timezone rather than assuming `CRON_TZ` is supported. If deploying the
existing Render web blueprint instead of this Windows installation, add one
Render Cron Job on schedule `0 1 * * *`, build with `uv sync --frozen`, run
`uv run python manage.py send_vaccination_reminders --type all`, and configure the
same DATABASE_URL and Semaphore secrets as the web service. The current Render
web-only blueprint does not itself create a scheduled job.

### Changed files and verification (2026-11-14)

- [Missed reminder service](vaccination/missed_appointments.py): next-day selection,
  recorded-consent checks, durable event claims, skip reasons and controlled retries.
- [Semaphore integration](vaccination/sms.py): confirmed acceptance, unknown outcomes,
  credential redaction and redirect prevention.
- [Models](vaccination/models.py): shared effective appointment status and SMS statuses.
- [Daily command](vaccination/management/commands/send_vaccination_reminders.py): Manila
  dates, explicit retry option and nonzero exit for failed/unknown submissions.
- [Views](vaccination/views.py) and [patient SMS template](templates/vaccination/sms_patient_detail.html):
  consistent missed status, date-specific reminder status and expandable saved messages.
- [Migration 0010](vaccination/migrations/0010_sms_outcome_statuses.py): status choices and
  evidence-based correction of legacy gateway-accepted missed messages.
- [Tests](vaccination/tests.py): mocked gateway, concurrency/crash, migration, consent,
  timezone, history and manual-workflow coverage; deterministic date fixtures.
- [Windows task configuration](scripts/configure_sms_scheduler.ps1) and this README:
  configurable 09:00 Philippine-time deployment and exact setup steps.

Verification: 201 vaccination tests passed (fast password hashing in the test process
only); 80 targeted reminder/SMS-page/report tests also passed using the normal test
configuration. Browser checks passed at mobile and desktop widths, including manual
SMS with the gateway disabled, invalid-contact skips and saved history. No real SMS
was sent. Local MySQL database `animalbite` has migration 0010 applied, its unique
event-key constraint verified, and all 11 existing SMS records retained.

Deployment activation remains administrator work: Windows denied S4U registration
from the non-elevated session. The existing 08:00 interactive-login task is unchanged;
run the script above in elevated PowerShell to activate 09:00 logged-out execution.

### 4. Monitoring

- **SMS Logs:** `/vaccination/sms/logs/` — filter by patient / case / type / status, see `sent_at`, `recipient`, `message`, `api_response`
- **Per-dose history:** `Vaccination Schedule → Manage` — shows past SMS per dose (sent/queued/failed) + manual send
- **Audit:** `AuditLog` entries `SEND_SMS`, `SEND_SMS_DRYRUN`, `TRIGGER_SMS`
- **Admin:** Django admin → `Vaccination → SMS Logs`

### 5. Message templates (Filipino)

```
3 days before: "ABTC Dumingag: Paalala, {Name} ({Case}) may vaccine schedule sa {Date} ({Dose}). Mangyaring pumunta sa RHU 3 araw mula ngayon. Dalhin ang card. Salamat! -RHUDumingag"
On day:       "ABTC Dumingag: Ngayon na ang vaccine schedule ni {Name} ({Case}) - {Dose} ngayong {Date}. Pumunta sa RHU ngayong araw. Dalhin ang vaccination card. -RHUDumingag"
```

Edit in `vaccination/sms.py:build_reminder_message`.

### 6. Troubleshooting

- `SEMAPHORE_API_KEY not configured` → set key + `SEMAPHORE_ENABLED=True`
- `Invalid PH number` → patient `contact_number` must be 11-digit `09...` or `639...`. Check `Patients → Edit`.
- `Already sent today` → duplicate guard prevents spam; `--force` applies only to advance/day reminders, never missed appointment events
- `Failed` / `Refunded` → check Semaphore dashboard credit balance & sender name approval (must be `RHUDumingag` and status Active)
- Do not start message with `TEST` — Semaphore silently ignores it (code prefixes a space if needed)