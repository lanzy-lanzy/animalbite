from django.db.models import Q

from bite_cases.models import AnimalBiteCase


def doctor_queue_cases(user):
    """Cases a doctor may work: unclaimed or already owned by that doctor."""
    return AnimalBiteCase.objects.filter(
        Q(clinical_assessment__isnull=True) |
        Q(clinical_assessment__assessed_by=user)
    )


def pending_assessment_count(user):
    """Queue cases still needing sign-off: no assessment or an unfinished draft."""
    return doctor_queue_cases(user).filter(
        Q(clinical_assessment__isnull=True) |
        Q(clinical_assessment__status='draft')
    ).count()
