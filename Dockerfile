FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libjpeg62-turbo libwebp7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements_lock.txt .
RUN pip install --no-cache-dir -r requirements_lock.txt

COPY . .

# Compile betterproto modules (proto/*.proto -> proto/*.py). The Bazel rule
# generates these into bazel-bin at dev time; for the runtime image we bake
# them in alongside the source. Imports like `from proto.comment import Comment`
# require the .py files to live next to the .proto sources.
RUN python tools/protoc_betterproto.py \
    proto/comment.proto proto/location.proto proto/media_item.proto \
    proto/post_record.proto proto/reaction.proto proto/reshared_from.proto \
    proto/

# Collect static files without a DB connection (SQLite in-memory mode)
RUN RUNNING_TESTS=1 SECRET_KEY=build-only DJANGO_SETTINGS_MODULE=django_config.settings \
    python manage.py collectstatic --noinput

EXPOSE 8000
