"""Single-user OAuth restriction: only BLOG_OWNER_EMAIL may log in."""
from django.conf import settings
from django.core.exceptions import PermissionDenied

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class SingleUserAccountAdapter(DefaultAccountAdapter):
    """Disallow registration via any form-based signup flow."""

    def is_open_for_signup(self, request):
        return False


class SingleUserSocialAdapter(DefaultSocialAccountAdapter):
    """Reject any Google account that is not the blog owner."""

    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get('email', '')
        if email != settings.ALLOWED_LOGIN_EMAIL:
            raise PermissionDenied("Not authorized")

    def is_open_for_signup(self, request, sociallogin):
        """Allow the owner to complete first-time social account creation."""
        email = sociallogin.account.extra_data.get('email', '')
        return email == settings.ALLOWED_LOGIN_EMAIL
