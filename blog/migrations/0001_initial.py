import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Tag',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, unique=True)),
                ('slug', models.SlugField(max_length=200, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name='Post',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(blank=True, max_length=500)),
                ('slug', models.SlugField(max_length=200, unique=True)),
                ('content_text', models.TextField(blank=True)),
                ('content_html', models.TextField(blank=True)),
                ('content_markdown', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('imported_at', models.DateTimeField(blank=True, null=True)),
                ('source', models.IntegerField(
                    choices=[(0, 'Invalid/Unknown'), (1, 'Blog'), (2, 'Google+'), (3, 'Facebook'), (4, 'Twitter')],
                    default=1,
                )),
                ('source_id', models.CharField(blank=True, db_index=True, max_length=500)),
                ('source_url', models.URLField(blank=True, max_length=1000)),
                ('visibility', models.IntegerField(
                    choices=[(1, 'Public'), (2, 'Unlisted'), (3, 'Private')],
                    default=1,
                )),
                ('location_name', models.CharField(blank=True, max_length=500)),
                ('location_lat', models.FloatField(blank=True, null=True)),
                ('location_lng', models.FloatField(blank=True, null=True)),
                ('reshared_from_author', models.CharField(blank=True, max_length=300)),
                ('reshared_from_url', models.URLField(blank=True, max_length=1000)),
                ('reaction_count', models.IntegerField(default=0)),
                ('comment_count', models.IntegerField(default=0)),
                ('media_count', models.IntegerField(default=0)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='PostMedia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('post', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='media', to='blog.post')),
                ('media_type', models.IntegerField(
                    choices=[(1, 'Image'), (2, 'Video'), (3, 'GIF'), (4, 'Link Embed')],
                )),
                ('file', models.FileField(blank=True, upload_to='posts/%Y/%m/')),
                ('original_url', models.URLField(blank=True, max_length=1000)),
                ('caption', models.TextField(blank=True)),
                ('position', models.IntegerField(default=0)),
                ('width', models.IntegerField(blank=True, null=True)),
                ('height', models.IntegerField(blank=True, null=True)),
                ('embed_title', models.CharField(blank=True, max_length=500)),
                ('embed_url', models.URLField(blank=True, max_length=1000)),
            ],
            options={
                'ordering': ['position'],
            },
        ),
        migrations.CreateModel(
            name='PostComment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('post', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='blog.post')),
                ('author_name', models.CharField(max_length=300)),
                ('author_url', models.URLField(blank=True, max_length=1000)),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField(blank=True, null=True)),
                ('source_id', models.CharField(blank=True, max_length=500)),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
        migrations.CreateModel(
            name='PostReaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('post', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reactions', to='blog.post')),
                ('reaction_type', models.IntegerField(
                    choices=[(1, '+1'), (2, 'Like'), (3, 'Retweet'), (10, 'Other')],
                )),
                ('user_name', models.CharField(max_length=300)),
                ('user_url', models.URLField(blank=True, max_length=1000)),
            ],
        ),
        migrations.CreateModel(
            name='PostTag',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('post', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='post_tags', to='blog.post')),
                ('tag', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='post_tags', to='blog.tag')),
            ],
        ),
        migrations.AddIndex(
            model_name='post',
            index=models.Index(fields=['source', 'source_id'], name='post_source_lookup'),
        ),
        migrations.AddIndex(
            model_name='post',
            index=models.Index(fields=['visibility', '-created_at'], name='post_public_feed'),
        ),
        migrations.AddConstraint(
            model_name='post',
            constraint=models.UniqueConstraint(
                condition=~models.Q(source_id=''),
                fields=['source', 'source_id'],
                name='unique_source_post',
            ),
        ),
        migrations.AddIndex(
            model_name='postreaction',
            index=models.Index(fields=['post'], name='reaction_post_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='posttag',
            unique_together={('post', 'tag')},
        ),
    ]
