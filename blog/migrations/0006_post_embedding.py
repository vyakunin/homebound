# Hand-written migration: pgvector extension + embedding columns on Post.
#
# The CREATE EXTENSION runs only on PostgreSQL; SQLite (used by the in-memory
# test DB) skips it. The VectorField column itself is declared unconditionally
# — SQLite stores it as TEXT-affinity and tests never read or write the value.

from django.db import migrations, models

import pgvector.django


def _enable_pgvector(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('CREATE EXTENSION IF NOT EXISTS vector')


def _noop(apps, schema_editor):
    # Leaving the extension installed on rollback is harmless and avoids
    # breaking other (future) pgvector-using objects in the same schema.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0005_add_profile_link'),
    ]

    operations = [
        migrations.RunPython(_enable_pgvector, _noop),
        migrations.AddField(
            model_name='post',
            name='embedding',
            field=pgvector.django.VectorField(blank=True, dimensions=512, null=True),
        ),
        migrations.AddField(
            model_name='post',
            name='content_hash',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name='post',
            name='embedding_model',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='post',
            name='embedded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
