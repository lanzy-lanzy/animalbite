import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from vaccination.models import VaccineDose

logger = logging.getLogger(__name__)


@receiver(post_save, sender=VaccineDose, dispatch_uid="notify_explicit_missed_schedule")
def notify_explicit_missed_schedule(sender, instance, raw=False, **kwargs):
    if raw or instance.dose_status != "missed" or not instance.scheduled_date:
        return
    dose_id = instance.pk

    def dispatch():
        from .utils import dispatch_missed_schedule
        try:
            dispatch_missed_schedule(dose_id)
        except VaccineDose.DoesNotExist:
            pass
        except Exception:
            # No patient details or gateway credentials in infrastructure logs.
            logger.error("Missed schedule notification requires review in SMS history.")

    transaction.on_commit(dispatch)
