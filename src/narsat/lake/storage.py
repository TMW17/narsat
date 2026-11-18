"""Object storage for the lake's files (COG rasters, STAC items, exports).

One small abstraction over ``pyarrow.fs`` so that the same code runs against a local
directory (tests, laptop without Docker) and any S3-compatible store (SeaweedFS in
Compose, AWS S3, Cloudflare R2 in the cloud). PyArrow ships both filesystems in its
wheels, so no extra dependency is needed.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pyarrow.fs as pafs

from narsat.config import LakeSettings


def resolve_uri(uri: str) -> str:
    """Normalise a lake URI. Relative ``file://`` paths are anchored at the working directory."""
    if uri.startswith("file://"):
        return "file://" + str(Path(uri[len("file://") :]).resolve())
    return uri.rstrip("/")


def s3_filesystem(settings: LakeSettings) -> pafs.S3FileSystem:
    options: dict[str, object] = {"region": settings.s3_region, "allow_bucket_creation": True}
    if settings.s3_endpoint:
        parsed = urlparse(settings.s3_endpoint)
        options["endpoint_override"] = parsed.netloc or parsed.path
        options["scheme"] = parsed.scheme or "http"
    if settings.s3_access_key and settings.s3_secret_key:
        options["access_key"] = settings.s3_access_key.get_secret_value()
        options["secret_key"] = settings.s3_secret_key.get_secret_value()
    return pafs.S3FileSystem(**options)


class LakeStorage:
    """Files under one base URI, addressed by relative keys such as ``imagery/<id>/post.tif``."""

    def __init__(self, base_uri: str, settings: LakeSettings) -> None:
        self.base_uri = resolve_uri(base_uri)
        if self.base_uri.startswith("s3://"):
            self._fs: pafs.FileSystem = s3_filesystem(settings)
            self._root = self.base_uri[len("s3://") :]
        else:
            self._fs = pafs.LocalFileSystem()
            self._root = self.base_uri[len("file://") :]

    @classmethod
    def assets(cls, settings: LakeSettings) -> LakeStorage:
        return cls(settings.assets_uri, settings)

    def _path(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"invalid lake key {key!r}")
        return f"{self._root}/{key}"

    def uri(self, key: str) -> str:
        return f"{self.base_uri}/{key}"

    def ensure_ready(self) -> None:
        """Create the bucket or directory if it does not exist yet."""
        self._fs.create_dir(self._root, recursive=True)

    def put_file(self, local_path: Path, key: str) -> str:
        path = self._path(key)
        self._fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
        pafs.copy_files(
            str(local_path),
            path,
            source_filesystem=pafs.LocalFileSystem(),
            destination_filesystem=self._fs,
        )
        return self.uri(key)

    def put_bytes(self, data: bytes, key: str) -> str:
        path = self._path(key)
        self._fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
        with self._fs.open_output_stream(path) as stream:
            stream.write(data)
        return self.uri(key)

    def read_bytes(self, key: str) -> bytes:
        with self._fs.open_input_file(self._path(key)) as handle:
            data: bytes = handle.read()
        return data

    def exists(self, key: str) -> bool:
        info = self._fs.get_file_info(self._path(key))
        return bool(info.type != pafs.FileType.NotFound)

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if self._fs.get_file_info(base).type == pafs.FileType.NotFound:
            return []
        selector = pafs.FileSelector(base, recursive=True)
        return sorted(
            info.path[len(self._root) + 1 :]
            for info in self._fs.get_file_info(selector)
            if info.type == pafs.FileType.File
        )

    def delete_prefix(self, prefix: str) -> None:
        base = self._path(prefix)
        if self._fs.get_file_info(base).type != pafs.FileType.NotFound:
            self._fs.delete_dir(base)
