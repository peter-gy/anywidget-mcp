"""Retain immutable widget attachments and transfer bounded byte ranges."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from exceptiongroup import ExceptionGroup
from typing_extensions import TypedDict

PROTOCOL_VERSION = 3
CHUNK_BYTES = 64 * 1024
INLINE_BYTES = 64 * 1024
SPOOL_BYTES = 1024 * 1024
BLOB_ID = re.compile(r"sha256:[0-9a-f]{64}")


class BlobRef(TypedDict):
    id: str
    byteLength: int


@dataclass
class _Blob:
    file: tempfile.SpooledTemporaryFile[bytes]
    size: int


@dataclass
class _Upload:
    file: tempfile.SpooledTemporaryFile[bytes]
    size: int
    received: int
    digest: Any


def validate_ref(value: object) -> BlobRef:
    if not isinstance(value, dict) or set(value) != {"id", "byteLength"}:
        raise ValueError("Widget attachment reference must contain id and byteLength")
    identifier, size = value["id"], value["byteLength"]
    if not isinstance(identifier, str) or BLOB_ID.fullmatch(identifier) is None:
        raise ValueError("Invalid widget attachment ID")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("Widget attachment byteLength must be a non-negative integer")
    return {"id": identifier, "byteLength": size}


class Attachments:
    """Own bytes retained by live models, deliveries, replays, and upload operations."""

    def __init__(self) -> None:
        self._blobs: dict[str, _Blob] = {}
        self._live: dict[str, dict[str, set[str]]] = {}
        self._latest: set[str] = set()
        self._pins: dict[str, int] = {}
        self._uploads: dict[tuple[int, str], _Upload] = {}
        self._uploaded: dict[int, set[str]] = {}

    @staticmethod
    def _file() -> tempfile.SpooledTemporaryFile[bytes]:
        return tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)

    @property
    def has_files(self) -> bool:
        return bool(self._blobs or self._uploads)

    def put(self, data: bytes) -> BlobRef:
        identifier = "sha256:" + hashlib.sha256(data).hexdigest()
        if identifier not in self._blobs:
            stream = self._file()
            try:
                if len(data) > SPOOL_BYTES:
                    stream.rollover()
                stream.write(data)
            except BaseException:
                stream.close()
                raise
            self._blobs[identifier] = _Blob(stream, len(data))
        return {"id": identifier, "byteLength": len(data)}

    def delivery(
        self,
        instance_id: str,
        payload: dict[str, Any],
        ids: Iterable[str],
        *,
        inline: bool = True,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        stream = self._file()
        digest = hashlib.sha256()
        size = 0
        try:
            for part in json.JSONEncoder(
                ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).iterencode(payload):
                encoded = part.encode("utf-8")
                if size <= SPOOL_BYTES < size + len(encoded):
                    stream.rollover()
                stream.write(encoded)
                digest.update(encoded)
                size += len(encoded)
            retained = set(ids)
            envelope: dict[str, Any] = {
                "protocolVersion": PROTOCOL_VERSION,
                "instanceId": instance_id,
            }
            if inline and size <= INLINE_BYTES:
                envelope["payload"] = payload
            else:
                identifier = "sha256:" + digest.hexdigest()
                if identifier not in self._blobs:
                    self._blobs[identifier] = _Blob(stream, size)
                    stream = None
                envelope["payloadRef"] = {"id": identifier, "byteLength": size}
                retained.add(identifier)
            self._latest.update(retained)
            return envelope, tuple(sorted(retained))
        finally:
            if stream is not None:
                stream.close()

    def read(self, identifier: str, offset: int) -> dict[str, Any]:
        blob = self._get(identifier)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= blob.size
        ):
            raise ValueError("Widget attachment offset is outside its byte range")
        blob.file.seek(offset)
        data = blob.file.read(CHUNK_BYTES)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "id": identifier,
            "offset": offset,
            "byteLength": blob.size,
            "data": base64.b64encode(data).decode("ascii"),
        }

    def resolve(self, value: object) -> bytes:
        ref = validate_ref(value)
        blob = self._get(ref["id"])
        if blob.size != ref["byteLength"]:
            raise ValueError(
                "Widget attachment byteLength does not match stored content"
            )
        blob.file.seek(0)
        return blob.file.read()

    def write(
        self, operation_id: int, identifier: str, size: int, offset: int, encoded: str
    ) -> dict[str, Any]:
        validate_ref({"id": identifier, "byteLength": size})
        if (
            isinstance(operation_id, bool)
            or not isinstance(operation_id, int)
            or not 1 <= operation_id <= 2**53 - 1
        ):
            raise ValueError("operation_id must be a positive safe integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Widget attachment offset must be a non-negative integer")
        if len(encoded) > 4 * ((CHUNK_BYTES + 2) // 3):
            raise ValueError("Widget attachment chunk exceeds 65536 bytes")
        data = base64.b64decode(encoded, validate=True)
        if (
            len(data) > CHUNK_BYTES
            or offset + len(data) > size
            or (not data and size != 0)
        ):
            raise ValueError("Invalid widget attachment chunk range")
        existing = self._blobs.get(identifier)
        if existing is not None:
            existing.file.seek(offset)
            if existing.size != size or existing.file.read(len(data)) != data:
                raise ValueError(
                    "Widget attachment retry does not match stored content"
                )
            partial = self._uploads.get((operation_id, identifier))
            if partial is not None:
                partial.file.close()
                self._uploads.pop((operation_id, identifier))
            self._uploaded.setdefault(operation_id, set()).add(identifier)
            return self._write_result(identifier, size, size, True)
        key = (operation_id, identifier)
        upload = self._uploads.get(key)
        if upload is None:
            if offset != 0:
                raise ValueError("Widget attachment upload must start at offset zero")
            upload = _Upload(self._file(), size, 0, hashlib.sha256())
            self._uploads[key] = upload
        if size != upload.size or offset > upload.received:
            raise ValueError("Widget attachment upload range is out of order")
        if offset < upload.received:
            upload.file.seek(offset)
            if (
                offset + len(data) > upload.received
                or upload.file.read(len(data)) != data
            ):
                raise ValueError(
                    "Widget attachment retry does not match received bytes"
                )
        else:
            upload.file.seek(offset)
            upload.file.write(data)
            upload.digest.update(data)
            upload.received += len(data)
        complete = upload.received == size
        if complete:
            if "sha256:" + upload.digest.hexdigest() != identifier:
                self.consume_uploads(operation_id)
                raise ValueError(
                    "Widget attachment content failed SHA-256 verification"
                )
            del self._uploads[key]
            self._blobs[identifier] = _Blob(upload.file, size)
            self._uploaded.setdefault(operation_id, set()).add(identifier)
        return self._write_result(identifier, size, upload.received, complete)

    @staticmethod
    def _write_result(
        identifier: str, size: int, received: int, complete: bool
    ) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "id": identifier,
            "byteLength": size,
            "received": received,
            "complete": complete,
        }

    def consume_uploads(self, operation_id: int) -> None:
        self._uploaded.pop(operation_id, None)
        errors: list[Exception] = []
        for key in tuple(self._uploads):
            if key[0] == operation_id:
                try:
                    self._uploads[key].file.close()
                except Exception as error:
                    errors.append(error)
                else:
                    self._uploads.pop(key)
        if errors:
            raise ExceptionGroup("Failed to close pending widget uploads", errors)
        self._prune()

    def retire_uploads_before(self, operation_id: int) -> None:
        pending = set(self._uploaded) | {key[0] for key in self._uploads}
        for earlier in sorted(pending):
            if earlier < operation_id:
                self.consume_uploads(earlier)

    def finish_delivery(self) -> None:
        self._latest.clear()
        self._prune()

    def pin(self, ids: Iterable[str]) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(ids))
        for identifier in result:
            self._get(identifier)
        for identifier in result:
            self._pins[identifier] = self._pins.get(identifier, 0) + 1
        return result

    def release(self, ids: Iterable[str]) -> None:
        for identifier in set(ids):
            count = self._pins.get(identifier, 0)
            if count <= 1:
                self._pins.pop(identifier, None)
            else:
                self._pins[identifier] = count - 1
        self._prune()

    def externalize(
        self,
        models: dict[str, dict[str, Any]],
        messages: list[dict[str, Any]],
        *,
        live_model_ids: set[str],
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], tuple[str, ...]]:
        references: set[str] = set()
        live = {
            key: dict(value)
            for key, value in self._live.items()
            if key in live_model_ids
        }

        def record(value: dict[str, Any], *, model: bool) -> dict[str, Any]:
            result = dict(value)
            data = value if model else value["data"]
            state = data.get("state")
            syncing = model or data.get("method") in {"update", "echo_update"}
            sources: dict[str, BlobRef] = {}
            changed: dict[str, set[str]] = {}
            if syncing and isinstance(state, dict):
                state = dict(state)
                changed = {name: set() for name in state}
                for name in ("_esm", "_css"):
                    source = state.get(name)
                    if isinstance(source, str):
                        sources[name] = self.put(source.encode("utf-8"))
                        references.add(sources[name]["id"])
                        changed[name].add(sources[name]["id"])
                        del state[name]
                if model:
                    result["state"] = state
                else:
                    result["data"] = {**data, "state": state}
            buffers: list[BlobRef] = []
            paths = data.get("bufferPaths" if model else "buffer_paths", [])
            for index, raw in enumerate(value.get("buffers", ())):
                ref = self.put(raw)
                references.add(ref["id"])
                buffers.append(ref)
                if (
                    syncing
                    and index < len(paths)
                    and paths[index]
                    and isinstance(paths[index][0], str)
                ):
                    changed.setdefault(paths[index][0], set()).add(ref["id"])
            result["buffers"] = buffers
            if sources:
                result["sourceRefs"] = sources
            if value["modelId"] in live_model_ids:
                live.setdefault(value["modelId"], {}).update(changed)
            return result

        try:
            wire_models = {
                key: record(value, model=True) for key, value in models.items()
            }
            wire_messages = [record(message, model=False) for message in messages]
        except BaseException:
            self._prune()
            raise
        self._live = live
        self._latest = references
        self._prune()
        return wire_models, wire_messages, tuple(sorted(references))

    def forget_models(self, ids: Iterable[str]) -> None:
        for identifier in ids:
            self._live.pop(identifier, None)
        self._prune()

    def _get(self, identifier: str) -> _Blob:
        if not isinstance(identifier, str) or BLOB_ID.fullmatch(identifier) is None:
            raise ValueError("Invalid widget attachment ID")
        blob = self._blobs.get(identifier)
        if blob is None:
            raise KeyError(f"Unknown widget attachment: {identifier}")
        return blob

    def _prune(self) -> None:
        retained = self._latest | set(self._pins)
        retained.update(
            identifier for ids in self._uploaded.values() for identifier in ids
        )
        retained.update(
            identifier
            for traits in self._live.values()
            for ids in traits.values()
            for identifier in ids
        )
        errors: list[Exception] = []
        for identifier in self._blobs.keys() - retained:
            try:
                self._blobs[identifier].file.close()
            except Exception as error:
                errors.append(error)
            else:
                self._blobs.pop(identifier)
        if errors:
            raise ExceptionGroup("Failed to release widget attachments", errors)

    def clear(self) -> None:
        errors: list[Exception] = []
        for identifier, blob in tuple(self._blobs.items()):
            try:
                blob.file.close()
            except Exception as error:
                errors.append(error)
            else:
                self._blobs.pop(identifier)
        for key, upload in tuple(self._uploads.items()):
            try:
                upload.file.close()
            except Exception as error:
                errors.append(error)
            else:
                self._uploads.pop(key)
        self._uploaded.clear()
        self._live.clear()
        self._latest.clear()
        self._pins.clear()
        if errors:
            raise ExceptionGroup("Failed to close widget attachment storage", errors)
