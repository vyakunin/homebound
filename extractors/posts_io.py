"""Length-delimited binary I/O for PostRecord proto messages.

File format: for each record, write a 4-byte big-endian unsigned integer
containing the serialised byte length, followed by the raw proto bytes.
File extension convention: .binpb
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Iterable, Iterator

from proto.comment import Comment
from proto.location import Location
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord
from proto.reaction import Reaction
from proto.reshared_from import ResharedFrom

# betterproto generates forward-reference annotations ("Location", "MediaItem",
# etc.) in post_record.py without cross-file imports, because all .proto files
# share an empty package declaration.  At runtime betterproto resolves these via
# get_type_hints(PostRecord, vars(proto.post_record)) — but those names are not
# in post_record's module namespace.  Inject them here so every PostRecord
# instantiation succeeds.
_post_record_mod = sys.modules["proto.post_record"]
for _name, _cls in [
    ("Comment", Comment),
    ("Location", Location),
    ("MediaItem", MediaItem),
    ("MediaType", MediaType),
    ("Reaction", Reaction),
    ("ResharedFrom", ResharedFrom),
]:
    if not hasattr(_post_record_mod, _name):
        setattr(_post_record_mod, _name, _cls)

_SIZE_FMT = ">I"  # big-endian uint32
_SIZE_LEN = struct.calcsize(_SIZE_FMT)


def write_records(records: Iterable[PostRecord], path: Path) -> int:
    """Write PostRecord messages to a length-delimited binary file.

    Returns the number of records written.
    """
    count = 0
    with open(path, "wb") as f:
        for record in records:
            data = bytes(record)
            f.write(struct.pack(_SIZE_FMT, len(data)))
            f.write(data)
            count += 1
    return count


def read_records(path: Path) -> Iterator[PostRecord]:
    """Yield PostRecord messages from a length-delimited binary file."""
    with open(path, "rb") as f:
        while True:
            size_bytes = f.read(_SIZE_LEN)
            if not size_bytes:
                break
            (size,) = struct.unpack(_SIZE_FMT, size_bytes)
            data = f.read(size)
            yield PostRecord().parse(data)
