# Hand-written migration: promote both vector columns from
# vector(512) (voyage-3-lite) to vector(1024) (voyage-3.5).
#
# Voyage 3.5 is materially better at cross-lingual recall — the
# observed bug was English bot queries missing relevant Russian
# posts. Spike on 10 EN queries × 8 RU seed posts + 50 distractors:
#   voyage-3-lite  → mean rank 1.3, hit@1 80%
#   voyage-3.5     → mean rank 1.1, hit@1 90%
# The lift moves the Venezuela/Trump 2026 query from rank 2 to rank 1,
# exactly the bug seen in production transcripts.
#
# Steps (Postgres-only):
#   1. Drop HNSW indexes from 0010 — they're tied to vector(512).
#   2. NULL out all embedding values — pgvector won't auto-resize.
#   3. ALTER COLUMN TYPE vector(1024) on both tables.
#   4. Recreate HNSW indexes at the new dim.
# Backfill is a separate step (not part of the migration; see the
# generate_embeddings + generate_chunk_embeddings management commands).

from django.db import migrations

import pgvector.django


def _to_1024(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS blog_postchunk_embedding_hnsw")
    schema_editor.execute("DROP INDEX IF EXISTS blog_post_embedding_hnsw")
    schema_editor.execute("UPDATE blog_postchunk SET embedding = NULL, embedding_model = '', embedded_at = NULL")
    schema_editor.execute("UPDATE blog_post      SET embedding = NULL, embedding_model = '', embedded_at = NULL")
    schema_editor.execute("ALTER TABLE blog_postchunk ALTER COLUMN embedding TYPE vector(1024)")
    schema_editor.execute("ALTER TABLE blog_post      ALTER COLUMN embedding TYPE vector(1024)")
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


def _to_512(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS blog_postchunk_embedding_hnsw")
    schema_editor.execute("DROP INDEX IF EXISTS blog_post_embedding_hnsw")
    schema_editor.execute("UPDATE blog_postchunk SET embedding = NULL, embedding_model = '', embedded_at = NULL")
    schema_editor.execute("UPDATE blog_post      SET embedding = NULL, embedding_model = '', embedded_at = NULL")
    schema_editor.execute("ALTER TABLE blog_postchunk ALTER COLUMN embedding TYPE vector(512)")
    schema_editor.execute("ALTER TABLE blog_post      ALTER COLUMN embedding TYPE vector(512)")
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


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0010_hnsw_indexes"),
    ]

    operations = [
        # Update Django's model state to know the column is now 1024d
        # WITHOUT issuing schema DDL. The DDL is done by the RunPython
        # below in raw SQL — Django's AlterField on a VectorField with
        # a different `dimensions` would try to ALTER COLUMN TYPE, and
        # pgvector refuses to auto-cast a populated 512d column to
        # 1024d. SeparateDatabaseAndState lets us split the state
        # update from the DDL.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="post",
                    name="embedding",
                    field=pgvector.django.VectorField(blank=True, dimensions=1024, null=True),
                ),
                migrations.AlterField(
                    model_name="postchunk",
                    name="embedding",
                    field=pgvector.django.VectorField(blank=True, dimensions=1024, null=True),
                ),
            ],
            database_operations=[],
        ),
        # Postgres-side raw DDL: drop HNSW → NULL out values → ALTER
        # column type → rebuild HNSW. SQLite test DB is a no-op.
        migrations.RunPython(_to_1024, _to_512),
    ]
