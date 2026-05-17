from django.urls import path

from blog import views
from blog.feeds import LatestPostsFeed

app_name = 'blog'

urlpatterns = [
    path('', views.PostListView.as_view(), name='post_list'),
    path('post/<slug:slug>/', views.PostDetailView.as_view(), name='post_detail'),
    path('post/<slug:slug>/edit/', views.PostUpdateView.as_view(), name='post_edit'),
    path('new/', views.PostCreateView.as_view(), name='post_create'),
    path('api/upload-image/', views.upload_image, name='upload_image'),
    path('api/search/semantic/', views.semantic_search_api, name='semantic_search_api'),
    path('api/bot/ask/', views.bot_ask_api, name='bot_ask_api'),
    path('bot/', views.BotWidgetView.as_view(), name='bot_widget'),
    path('tag/<slug:slug>/', views.TagView.as_view(), name='tag'),
    path('source/<str:name>/', views.SourceView.as_view(), name='source'),
    path('search/', views.SearchView.as_view(), name='search'),
    path('word-cloud/', views.WordCloudView.as_view(), name='word_cloud'),
    path('feed/', LatestPostsFeed(), name='rss_feed'),
]
