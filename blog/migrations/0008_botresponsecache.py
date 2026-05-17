# Phase 4.x: response-level cache for the public bot.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0007_bottranscript'),
    ]

    operations = [
        migrations.CreateModel(
            name='BotResponseCache',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_hit_at', models.DateTimeField(auto_now=True)),
                ('hit_count', models.IntegerField(default=1)),
                ('prompt_hash', models.CharField(db_index=True, max_length=64)),
                ('context_hash', models.CharField(db_index=True, max_length=64)),
                ('model', models.CharField(max_length=64)),
                ('question', models.TextField()),
                ('answer', models.TextField()),
                ('cited_slugs', models.JSONField(default=list)),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(
                        fields=['prompt_hash', 'context_hash', 'model'],
                        name='botcache_prompt_ctx_model',
                    ),
                ],
                'indexes': [
                    models.Index(fields=['prompt_hash', 'context_hash'], name='botcache_lookup'),
                ],
            },
        ),
    ]
