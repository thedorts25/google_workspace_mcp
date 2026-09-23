"""
MICADOR CUSTOM: Company Brain tools for plain-text (.md) pages in Google Drive. v4.

Micador-owned module. Registered by a single commented import at the foot of
gdrive/drive_tools.py so an upstream update can remove at most one line.

Every write tool is fenced to approved Brain folders set in the environment:

    MICADOR_BRAIN_WRITE_ROOTS="operations:<folderId>,company:<folderId>"

A target is writable only if one of those folder IDs is the target itself or
one of its ancestors. If the variable is empty or unset, every write is
refused (fail closed). Set it to "*" to allow writes anywhere the user's own
Drive permissions allow (Micador production setting, Sep 2026); the skill
then carries the placement rules. If a page's frontmatter declares `tier:`, it must match
the tier of the root it is being written under.

Tools:
  create_brain_file          create a plain-text file under an approved root
  create_brain_folder        create a folder under an approved root
  write_drive_text_file      replace or append in place (same file ID), optional
                             modifiedTime precondition, post-write collision
                             check, pins the new revision
  restore_drive_file_revision  write an earlier revision's content back as the
                             new head (recovery)
  pin_drive_file_revision    keepForever on a revision (retention)
  get_drive_file_state       modifiedTime, last editor, md5, head revision
  list_drive_file_revisions  revision history with each revision's editor

Deliberately NOT provided: delete, trash, move, share, permission changes.
"""

import asyncio
import io
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors
from gdrive.drive_helpers import resolve_drive_item

logger = logging.getLogger(__name__)

STATE_FIELDS = (
    "id, name, mimeType, parents, driveId, modifiedTime, "
    "lastModifyingUser(displayName, emailAddress), md5Checksum, size, "
    "version, headRevisionId, webViewLink"
)
REVISION_FIELDS = (
    "revisions(id, modifiedTime, lastModifyingUser(displayName, emailAddress), "
    "md5Checksum, size, keepForever)"
)
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_ANCESTOR_DEPTH = 25
ROOTS_ENV = "MICADOR_BRAIN_WRITE_ROOTS"
TEXT_MIME_TYPES = {"text/markdown", "text/plain"}
# Existing files may carry either markdown MIME type depending on how they were uploaded.
WRITABLE_MIME_TYPES = TEXT_MIME_TYPES | {"text/x-markdown"}


# ---------------------------------------------------------------- fence

def _allowed_roots() -> Dict[str, str]:
    """Return {folder_id: tier} from MICADOR_BRAIN_WRITE_ROOTS."""
    raw = os.getenv(ROOTS_ENV, "").strip()
    roots: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"{ROOTS_ENV} entry '{part}' must be tier:folderId")
        tier, folder_id = part.split(":", 1)
        roots[folder_id.strip()] = tier.strip().lower()
    return roots


async def _ancestors(service, item_id: str) -> List[str]:
    """IDs from the item itself up through its parents (first parent each hop)."""
    chain = [item_id]
    current = item_id
    for _ in range(MAX_ANCESTOR_DEPTH):
        meta = await asyncio.to_thread(
            service.files()
            .get(fileId=current, fields="id, parents", supportsAllDrives=True)
            .execute
        )
        parents = meta.get("parents") or []
        if not parents:
            break
        current = parents[0]
        chain.append(current)
    return chain


UNFENCED = "*"


async def _require_inside_fence(service, item_id: str) -> str:
    """Raise unless item_id is, or sits under, an approved root. Returns the tier.

    MICADOR_BRAIN_WRITE_ROOTS="*" switches the fence off: writes are allowed
    anywhere the signed-in user's own Drive permissions allow (tier "any").
    """
    if os.getenv(ROOTS_ENV, "").strip() == UNFENCED:
        return "any"
    roots = _allowed_roots()
    if not roots:
        raise PermissionError(
            f"Brain writes are disabled: {ROOTS_ENV} is not configured on this connector."
        )
    for ancestor in await _ancestors(service, item_id):
        if ancestor in roots:
            return roots[ancestor]
    raise PermissionError(
        f"Refusing to write: {item_id} is not inside an approved Brain folder."
    )


_FRONTMATTER_TIER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _declared_tier(content: str) -> Optional[str]:
    match = _FRONTMATTER_TIER.match(content or "")
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.strip().lower().startswith("tier:"):
            return line.split(":", 1)[1].strip().strip("'\"").lower() or None
    return None


def _check_tier(content: str, root_tier: str) -> None:
    if root_tier == "any":
        return
    declared = _declared_tier(content)
    if declared and declared != root_tier:
        raise PermissionError(
            f"Refusing to write: page declares tier '{declared}' but the destination "
            f"folder is tier '{root_tier}'."
        )


# ---------------------------------------------------------------- helpers

def _format_state(meta: Dict[str, Any]) -> str:
    user = meta.get("lastModifyingUser") or {}
    return "\n".join(
        [
            f"File: {meta.get('name')} (ID: {meta.get('id')})",
            f"   MIME type: {meta.get('mimeType')}",
            f"   Modified: {meta.get('modifiedTime')}",
            f"   Last modified by: {user.get('displayName', '?')} <{user.get('emailAddress', '?')}>",
            f"   md5Checksum: {meta.get('md5Checksum')}",
            f"   Size: {meta.get('size')}",
            f"   Version: {meta.get('version')}",
            f"   Head revision: {meta.get('headRevisionId')}",
            f"   Shared drive ID: {meta.get('driveId', '(My Drive)')}",
            f"   Link: {meta.get('webViewLink')}",
        ]
    )


async def _get_state(service, file_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(
        service.files()
        .get(fileId=file_id, fields=STATE_FIELDS, supportsAllDrives=True)
        .execute
    )


async def _download(request) -> bytes:
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = await asyncio.to_thread(downloader.next_chunk)
    return buffer.getvalue()


async def _download_text(service, file_id: str) -> str:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    return (await _download(request)).decode("utf-8")


async def _list_revisions(service, file_id: str) -> List[Dict[str, Any]]:
    """All revisions, following nextPageToken until exhausted."""
    revisions: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    for _ in range(100):  # hard stop against a misbehaving API
        kwargs: Dict[str, Any] = {
            "fileId": file_id,
            "fields": "nextPageToken, " + REVISION_FIELDS,
            "pageSize": 200,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = await asyncio.to_thread(service.revisions().list(**kwargs).execute)
        revisions.extend(result.get("revisions", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return revisions


async def _pin(service, file_id: str, revision_id: str) -> None:
    await asyncio.to_thread(
        service.revisions()
        .update(fileId=file_id, revisionId=revision_id, body={"keepForever": True})
        .execute
    )


def _collision_note(revisions: List[Dict[str, Any]], expected_prev: Optional[str], new_head: Optional[str]) -> Optional[str]:
    """If the revision just before ours is not the one we read, another edit landed in between.

    Returns a COLLISION CHECK INCOMPLETE warning when the check cannot be made,
    never silence.
    """
    ids = [r.get("id") for r in revisions]
    if not expected_prev or not new_head or new_head not in ids:
        return (
            "COLLISION CHECK INCOMPLETE: could not locate the previous head "
            f"({expected_prev}) and the new revision ({new_head}) in revision history. "
            "Treat this write as unverified and compare revisions manually."
        )
    idx = ids.index(new_head)
    actual_prev = ids[idx - 1] if idx > 0 else None
    if actual_prev != expected_prev:
        return (
            f"COLLISION: another revision ({actual_prev}) landed between the read "
            f"(head {expected_prev}) and this write ({new_head}). Their change may be "
            "overwritten. Compare revisions and restore if needed."
        )
    return None


async def _upload(service, file_id: str, data: bytes, mime: str) -> Dict[str, Any]:
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime or "text/plain", resumable=False)
    return await asyncio.to_thread(
        service.files()
        .update(fileId=file_id, media_body=media, fields=STATE_FIELDS, supportsAllDrives=True)
        .execute
    )


# ---------------------------------------------------------------- implementations

async def _write_drive_text_file_impl(
    service,
    file_id: str,
    content: str,
    mode: str = "replace",
    expected_modified_time: Optional[str] = None,
    pin_revision: bool = True,
) -> str:
    if mode not in ("replace", "append"):
        raise ValueError("mode must be 'replace' or 'append'.")
    if content is None:
        raise ValueError("content is required.")

    resolved_id, _ = await resolve_drive_item(service, file_id)
    root_tier = await _require_inside_fence(service, resolved_id)
    current = await _get_state(service, resolved_id)
    mime = current.get("mimeType", "")

    if mime.startswith(GOOGLE_NATIVE_PREFIX):
        raise ValueError(
            f"Refusing to write: {current.get('name')} is a Google-native file ({mime}). "
            "This tool only writes plain UTF-8 text files such as .md or .txt."
        )
    if mime not in WRITABLE_MIME_TYPES:
        raise ValueError(
            f"Refusing to write: {current.get('name')} has MIME type {mime}. "
            f"Only {sorted(WRITABLE_MIME_TYPES)} files can be written."
        )

    if mode == "replace" and not expected_modified_time:
        raise ValueError(
            "expected_modified_time is required for replace: read the file's state first "
            "(get_drive_file_state) and pass its modifiedTime, so another person's edit "
            "is never silently overwritten."
        )

    if expected_modified_time and current.get("modifiedTime") != expected_modified_time:
        user = current.get("lastModifyingUser") or {}
        raise RuntimeError(
            "CONFLICT: file changed since it was read. "
            f"Expected modifiedTime {expected_modified_time}, found {current.get('modifiedTime')} "
            f"(last modified by {user.get('emailAddress', '?')}). Re-read the file and retry."
        )

    if mode == "append":
        existing = await _download_text(service, resolved_id)
        seam = "" if (not existing or existing.endswith("\n") or content.startswith("\n")) else "\n"
        new_text = existing + seam + content
    else:
        _check_tier(content, root_tier)
        new_text = content

    data = new_text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"Refusing to write {len(data)} bytes; limit is {MAX_TEXT_BYTES}.")

    updated = await _upload(service, resolved_id, data, mime)

    notes: List[str] = []
    new_head = updated.get("headRevisionId")
    try:
        revisions = await _list_revisions(service, resolved_id)
        collision = _collision_note(revisions, current.get("headRevisionId"), new_head)
        if collision:
            notes.append(collision)
    except Exception as exc:  # the write itself succeeded; report, don't hide
        notes.append(f"COLLISION CHECK INCOMPLETE: could not read revisions: {exc}")

    if pin_revision and new_head:
        try:
            await _pin(service, resolved_id, new_head)
            notes.append(f"Pinned revision {new_head} (keepForever).")
        except Exception as exc:
            notes.append(f"WARNING: could not pin revision {new_head}: {exc}")

    return "\n".join(
        [
            f"✅ {mode.capitalize()} OK: {updated.get('name')} (same file ID {resolved_id})",
            f"   Tier: {root_tier}",
            f"   Previous modifiedTime: {current.get('modifiedTime')}",
            f"   Previous head revision: {current.get('headRevisionId')}",
            f"   Bytes written: {len(data)}",
            _format_state(updated),
            *notes,
        ]
    )


async def _create_brain_file_impl(
    service,
    parent_folder_id: str,
    file_name: str,
    content: str,
    mime_type: str = "text/markdown",
) -> str:
    if mime_type not in TEXT_MIME_TYPES:
        raise ValueError(f"mime_type must be one of {sorted(TEXT_MIME_TYPES)}.")
    resolved_parent, parent_meta = await resolve_drive_item(service, parent_folder_id)
    root_tier = await _require_inside_fence(service, resolved_parent)
    _check_tier(content, root_tier)
    data = (content or "").encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"Refusing to write {len(data)} bytes; limit is {MAX_TEXT_BYTES}.")
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    created = await asyncio.to_thread(
        service.files()
        .create(
            body={"name": file_name, "parents": [resolved_parent], "mimeType": mime_type},
            media_body=media,
            fields=STATE_FIELDS,
            supportsAllDrives=True,
        )
        .execute
    )
    return "\n".join([f"✅ Created {file_name} (tier {root_tier})", _format_state(created)])


async def _create_brain_folder_impl(service, parent_folder_id: str, folder_name: str) -> str:
    resolved_parent, _ = await resolve_drive_item(service, parent_folder_id)
    root_tier = await _require_inside_fence(service, resolved_parent)
    created = await asyncio.to_thread(
        service.files()
        .create(
            body={"name": folder_name, "parents": [resolved_parent], "mimeType": FOLDER_MIME},
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute
    )
    return f"✅ Created folder {created.get('name')} (ID: {created.get('id')}, tier {root_tier})\n   Link: {created.get('webViewLink')}"


async def _restore_drive_file_revision_impl(
    service,
    file_id: str,
    revision_id: str,
    expected_modified_time: Optional[str] = None,
) -> str:
    if not expected_modified_time:
        raise ValueError(
            "expected_modified_time is required for restore (it replaces the current head)."
        )
    resolved_id, _ = await resolve_drive_item(service, file_id)
    await _require_inside_fence(service, resolved_id)
    current = await _get_state(service, resolved_id)
    if current.get("mimeType") not in WRITABLE_MIME_TYPES:
        raise ValueError(
            f"Refusing to restore: {current.get('name')} has MIME type {current.get('mimeType')}."
        )
    request = service.revisions().get_media(fileId=resolved_id, revisionId=revision_id)
    old_text = (await _download(request)).decode("utf-8")
    result = await _write_drive_text_file_impl(
        service, resolved_id, old_text, "replace", expected_modified_time, True
    )
    return f"Restored content of revision {revision_id} as the new head.\n{result}"


async def _pin_drive_file_revision_impl(service, file_id: str, revision_id: str) -> str:
    resolved_id, _ = await resolve_drive_item(service, file_id)
    await _require_inside_fence(service, resolved_id)
    await _pin(service, resolved_id, revision_id)
    pinned = sum(1 for r in await _list_revisions(service, resolved_id) if r.get("keepForever"))
    warn = " WARNING: approaching Drive's 200 pinned-revision limit." if pinned >= 150 else ""
    return f"✅ Pinned revision {revision_id} on {resolved_id}. Pinned revisions now: {pinned}.{warn}"


async def _list_drive_file_revisions_impl(
    service,
    file_id: str,
    revision_id_for_content: Optional[str] = None,
) -> str:
    resolved_id, _ = await resolve_drive_item(service, file_id)
    revisions = await _list_revisions(service, resolved_id)
    lines = [f"Revisions for {resolved_id}: {len(revisions)}"]
    for rev in revisions:
        user = rev.get("lastModifyingUser") or {}
        lines.append(
            f"- {rev.get('id')} | {rev.get('modifiedTime')} | "
            f"{user.get('emailAddress', '?')} | md5 {rev.get('md5Checksum')} | "
            f"{rev.get('size')} bytes | keepForever={rev.get('keepForever')}"
        )
    if revision_id_for_content:
        request = service.revisions().get_media(fileId=resolved_id, revisionId=revision_id_for_content)
        lines.append("")
        lines.append(f"--- Content of revision {revision_id_for_content} ---")
        lines.append((await _download(request)).decode("utf-8", errors="replace"))
    return "\n".join(lines)


# ---------------------------------------------------------------- tools

@server.tool()
@handle_http_errors("create_brain_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def create_brain_file(
    service,
    user_google_email: str,
    parent_folder_id: str,
    file_name: str,
    content: str,
    mime_type: str = "text/markdown",
) -> str:
    """
    MICADOR: Create a plain-text Brain page (.md or .txt) inside an approved Brain
    folder. Refused outside MICADOR_BRAIN_WRITE_ROOTS, and refused if the page's
    frontmatter `tier:` does not match the destination tier.

    Args:
        user_google_email (str): The user's Google email address. Required.
        parent_folder_id (str): Folder to create the file in (must be inside an approved root).
        file_name (str): File name, e.g. 'supplier-x.md'.
        content (str): Full UTF-8 text content.
        mime_type (str): 'text/markdown' (default) or 'text/plain'.
    """
    return await _create_brain_file_impl(service, parent_folder_id, file_name, content, mime_type)


@server.tool()
@handle_http_errors("create_brain_folder", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def create_brain_folder(
    service,
    user_google_email: str,
    parent_folder_id: str,
    folder_name: str,
) -> str:
    """
    MICADOR: Create a folder inside an approved Brain folder.

    Args:
        user_google_email (str): The user's Google email address. Required.
        parent_folder_id (str): Parent folder (must be inside an approved root).
        folder_name (str): New folder name.
    """
    return await _create_brain_folder_impl(service, parent_folder_id, folder_name)


@server.tool()
@handle_http_errors("write_drive_text_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def write_drive_text_file(
    service,
    user_google_email: str,
    file_id: str,
    content: str,
    mode: str = "replace",
    expected_modified_time: Optional[str] = None,
    pin_revision: bool = True,
) -> str:
    """
    MICADOR: Replace or append the content of an existing plain-text Brain page in
    place (same file ID, sharing and links). Only inside approved Brain folders.
    Refuses Google-native Docs/Sheets/Slides.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): ID of the existing text file.
        content (str): Text to write (replace) or add to the end (append).
        mode (str): 'replace' (default) or 'append'.
        expected_modified_time (Optional[str]): modifiedTime last read via
            get_drive_file_state. REQUIRED for mode='replace'. If the file changed
            since, the write is refused with CONFLICT.
        pin_revision (bool): Pin the new revision (keepForever) so Drive does not
            purge it. Default True.

    Returns:
        str: New state, the previous head revision, and a COLLISION warning if
        another edit landed between read and write.
    """
    return await _write_drive_text_file_impl(
        service, file_id, content, mode, expected_modified_time, pin_revision
    )


@server.tool()
@handle_http_errors("restore_drive_file_revision", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def restore_drive_file_revision(
    service,
    user_google_email: str,
    file_id: str,
    revision_id: str,
    expected_modified_time: Optional[str] = None,
) -> str:
    """
    MICADOR: Recovery. Write an earlier revision's content back as the new head of
    a Brain page. The overwritten head stays in revision history.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): ID of the Brain page.
        revision_id (str): Revision whose content to restore.
        expected_modified_time (Optional[str]): REQUIRED. modifiedTime of the current head.
    """
    return await _restore_drive_file_revision_impl(service, file_id, revision_id, expected_modified_time)


@server.tool()
@handle_http_errors("pin_drive_file_revision", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def pin_drive_file_revision(
    service,
    user_google_email: str,
    file_id: str,
    revision_id: str,
) -> str:
    """
    MICADOR: Retention. Mark a Brain page revision keepForever (Drive allows up to
    200 per file) so it is not purged. Used by the sweep for external edits.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): ID of the Brain page.
        revision_id (str): Revision to pin.
    """
    return await _pin_drive_file_revision_impl(service, file_id, revision_id)


@server.tool()
@handle_http_errors("get_drive_file_state", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_drive_file_state(
    service,
    user_google_email: str,
    file_id: str,
) -> str:
    """
    MICADOR: Return a file's modifiedTime, last modifying user, md5Checksum, size,
    version and head revision. Use before any write (expected_modified_time) and
    to detect edits made outside Claude.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): ID of the file.
    """
    resolved_id, _ = await resolve_drive_item(service, file_id)
    return _format_state(await _get_state(service, resolved_id))


@server.tool()
@handle_http_errors("list_drive_file_revisions", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def list_drive_file_revisions(
    service,
    user_google_email: str,
    file_id: str,
    revision_id_for_content: Optional[str] = None,
) -> str:
    """
    MICADOR: List a plain file's revisions with the editor of each revision, and
    optionally return one revision's text (for diffs of external edits).

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): ID of the file.
        revision_id_for_content (Optional[str]): If given, include that revision's text.
    """
    return await _list_drive_file_revisions_impl(service, file_id, revision_id_for_content)
