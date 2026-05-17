from django.conf import settings


def analytics(request):
    return {'analytics_script': getattr(settings, 'ANALYTICS_SCRIPT', '')}


def bot_settings(request):
    """Expose ``BOT_PUBLIC`` so base.html can show/hide the footer link
    without each view having to thread it through."""
    return {'BOT_PUBLIC': getattr(settings, 'BOT_PUBLIC', False)}
