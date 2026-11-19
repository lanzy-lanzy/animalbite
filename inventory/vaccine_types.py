from django.db.models import Q


def tetanus_stock_filter():
    """Identify explicitly named Tetanus stock, never a generic rabies lot."""
    return Q(vaccine_name__icontains='tetanus') | Q(vaccine_name__iexact='TT')
