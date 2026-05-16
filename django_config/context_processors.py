from django.conf import settings


def analytics(request):
    return {'analytics_script': getattr(settings, 'ANALYTICS_SCRIPT', '')}
