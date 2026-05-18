# Hand-written migration: PostChunk table for chunk-level embeddings.
#
# Phase 2 anti-long-doc-bias: each Post is split into ~800-char chunks;
# each chunk gets its own Voyage embedding. Retrieval cosine-matches the
# query against all chunks then max-pools to the parent post, so a 5K-char
# post's diffuse single-vector no longer beats a tightly focused 200-char
# post in semantic search.
#
# The existing Post.embedding column stays in place but is no longer read
# by the retrieval path after this lands; keeping the column makes
# rollback cheap (one swap in bot_retrieval._semantic_hits).

from django.db import migrations, models

import django.db.models.deletion
import pgvector.django


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0008_botresponsecache'),
    ]

    operations = [
        migrations.CreateModel(
            name='PostChunk',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('chunk_index', models.IntegerField()),
                ('text', models.TextField()),
                ('embedding', pgvector.django.VectorField(blank=True, dimensions=512, null=True)),
                ('content_hash', models.CharField(blank=True, max_length=64)),
                ('embedding_model', models.CharField(blank=True, max_length=64)),
                ('embedded_at', models.DateTimeField(blank=True, null=True)),
                ('post', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='chunks',
                    to='blog.post',
                )),
            ],
            options={
                'ordering': ['post', 'chunk_index'],
            },
        ),
        migrations.AddIndex(
            model_name='postchunk',
            index=models.Index(fields=['post'], name='chunk_post_lookup'),
        ),
        migrations.AddConstraint(
            model_name='postchunk',
            constraint=models.UniqueConstraint(
                fields=('post', 'chunk_index'), name='unique_post_chunk',
            ),
        ),
    ]
