from django.conf import settings
from django.db import models


class Notification(models.Model):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name="appointment_notifications")
    dose = models.ForeignKey("vaccination.VaccineDose", on_delete=models.CASCADE)
    scheduled_date = models.DateField()
    kind = models.CharField(max_length=30, default="missed_schedule")
    message = models.TextField()
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(
            fields=["recipient", "dose", "scheduled_date", "kind"],
            name="unique_missed_schedule_notice",
        )]
