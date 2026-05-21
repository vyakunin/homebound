# Hand-written migration: HNSW indexes on the two pgvector columns
# (PostChunk.embedding for the live retrieval path, Post.embedding for
# the pre-chunked legacy fallback).
#
# At ~12k chunks / ~10k posts a sequential cosine scan is still sub-second,
# but the index is fire-and-forget: builds in well under a second, costs
# ~5-10x the vector data in disk, and keeps query latency flat as the
# corpus grows.
#
# m=16 / ef_construction=64 are the pgvector defaults — fine for our
# scale. ef_search (query-time) stays at the default 40; bump per-session
# with `SET hnsw.ef_search = 100` if recall@k ever drops.
#
# NULL embeddings are skipped by HNSW natively, so no partial-index WHERE
# clause is needed.

from django.db import migrations


def _create_hnsw(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS blog_postchunk_embedding_hnsw "
        "ON blog_postchunk USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS blog_post_embedding_hnsw "
        "ON blog_post USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def _drop_hnsw(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS blog_postchunk_embedding_hnsw")
    schema_editor.execute("DROP INDEX IF EXISTS blog_post_embedding_hnsw")


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0009_postchunk"),
    ]

    operations = [
        migrations.RunPython(_create_hnsw, _drop_hnsw),
    ]
