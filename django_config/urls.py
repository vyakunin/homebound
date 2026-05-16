from django.contrib import admin
from django.contrib.sitemaps.views import sitemap, index
from django.http import HttpResponse
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.decorators.cache import cache_control
from django.views.generic import RedirectView

from blog.sitemaps import build_sitemaps

_sitemaps = build_sitemaps()


def robots_txt(request):
    lines = [
        'User-agent: *',
        'Allow: /',
        f'Sitemap: {request.scheme}://{request.get_host()}/sitemap.xml',
    ]
    return HttpResponse('\n'.join(lines), content_type='text/plain')


# Cache sitemap responses at the CDN edge for 1h. Sitemaps over 11k posts
# do a full table scan per render (~3s before sharding, <500ms per year shard);
# cache prevents repeated origin hits when bots re-crawl.
_cached_index = cache_control(public=True, max_age=3600)(index)
_cached_sitemap = cache_control(public=True, max_age=3600)(sitemap)


urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/static/img/thumbnail.png', permanent=True)),
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path(
        'sitemap.xml',
        _cached_index,
        {'sitemaps': _sitemaps, 'sitemap_url_name': 'sitemaps'},
    ),
    path(
        'sitemap-<section>.xml',
        _cached_sitemap,
        {'sitemaps': _sitemaps},
        name='sitemaps',
    ),
    path('robots.txt', robots_txt),
    path('', include('blog.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
