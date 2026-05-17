# Phase 4: persistent log of every public-bot exchange, for the user's
# sign-off review (~30 sample transcripts) before the widget goes public.
#
# Stores only IP HASHES, not raw IPs — see blog/bot_throttle.py for the
# hashing rule. cited_slugs is a JSON array so SQLite (test DB) and
# Postgres both work without an ArrayField migration.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0006_post_embedding'),
    ]

    operations = [
        migrations.CreateModel(
            name='BotTranscript',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('ip_hash', models.CharField(db_index=True, max_length=64)),
                ('session_token', models.CharField(blank=True, db_index=True, max_length=64)),
                ('question', models.TextField()),
                ('answer', models.TextField(blank=True)),
                ('cited_slugs', models.JSONField(default=list)),
                ('model', models.CharField(blank=True, max_length=64)),
                ('input_tokens', models.IntegerField(default=0)),
                ('output_tokens', models.IntegerField(default=0)),
                ('cache_read_input_tokens', models.IntegerField(default=0)),
                ('latency_ms', models.IntegerField(default=0)),
                ('error', models.TextField(blank=True)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['ip_hash', '-created_at'], name='bottx_ip_recent'),
                ],
            },
        ),
    ]
