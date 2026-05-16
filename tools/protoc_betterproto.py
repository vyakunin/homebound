"""Bazel tool: run grpc_tools.protoc with the betterproto output plugin.

Invoked by the py_betterproto_library genrule as:
    protoc_betterproto <proto_file>... <out_dir>

All proto files must be in the same directory.  We use that directory as the
protoc include path (-I<proto_dir>) so betterproto generates flat module names
(e.g. media_item.py) directly in <out_dir> rather than a nested package.

grpcio-tools bundles well-known proto files (google/protobuf/timestamp.proto
etc.) under grpc_tools/_proto/. We add that directory as a second include path
so Timestamp imports resolve correctly.

The betterproto plugin is invoked via a temp wrapper script that carries the
current PYTHONPATH, since protoc doesn't forward environment variables to
plugin subprocesses.
"""
import os
import stat
import sys
import tempfile

import grpc_tools
from grpc_tools import protoc


def main() -> None:
    *proto_files, out_dir = sys.argv[1:]

    proto_include = os.path.join(os.path.dirname(grpc_tools.__file__), '_proto')
    python_path = os.pathsep.join(p for p in sys.path if p)

    # Use the directory containing the proto files as the include root so that
    # betterproto generates flat module names (comment.py, not proto/comment.py).
    proto_dir = os.path.dirname(proto_files[0]) if proto_files else '.'
    bare_files = [os.path.basename(f) for f in proto_files]

    wrapper_fd, wrapper_path = tempfile.mkstemp(suffix='.sh', prefix='bp_plugin_')
    try:
        with os.fdopen(wrapper_fd, 'w') as f:
            f.write('#!/bin/sh\n')
            f.write(f'exec env PYTHONPATH="{python_path}" "{sys.executable}" -m betterproto.plugin "$@"\n')
        os.chmod(wrapper_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        rc = protoc.main([
            "grpc_tools.protoc",
            f"-I{proto_dir}",    # include path = proto/ dir, so filenames resolve directly
            f"-I{proto_include}",  # well-known protos (Timestamp etc.)
            f"--plugin=protoc-gen-python_betterproto={wrapper_path}",
            f"--python_betterproto_out={out_dir}",
            *bare_files,         # just filenames, e.g. "comment.proto", not "proto/comment.proto"
        ])
    finally:
        os.unlink(wrapper_path)

    sys.exit(rc)


if __name__ == "__main__":
    main()
