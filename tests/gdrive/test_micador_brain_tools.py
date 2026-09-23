"""MICADOR: tests for gdrive/micador_brain_tools.py v2 (Company Brain write path)."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gdrive import micador_brain_tools as mbt  # noqa: E402

ROOTS = "operations:OPS_ROOT,company:CO_ROOT"

STATE = {
    "id": "f1",
    "name": "page.md",
    "mimeType": "text/markdown",
    "modifiedTime": "2026-10-01T00:00:00.000Z",
    "lastModifyingUser": {"displayName": "Ben", "emailAddress": "ben@example.com"},
    "md5Checksum": "abc",
    "size": "10",
    "version": "3",
    "headRevisionId": "r3",
    "webViewLink": "https://drive/f1",
}
UPDATED = {**STATE, "modifiedTime": "2026-10-02T00:00:00.000Z", "headRevisionId": "r4"}
MT = STATE["modifiedTime"]


def _service(state=None, updated=None, revisions=None):
    svc = MagicMock()
    svc.files.return_value.get.return_value.execute = MagicMock(return_value=state or STATE)
    svc.files.return_value.update.return_value.execute = MagicMock(return_value=updated or UPDATED)
    svc.files.return_value.create.return_value.execute = MagicMock(return_value={**STATE, "id": "new"})
    svc.revisions.return_value.list.return_value.execute = MagicMock(
        return_value={"revisions": revisions if revisions is not None else [{"id": "r3"}, {"id": "r4"}]}
    )
    svc.revisions.return_value.update.return_value.execute = MagicMock(return_value={})
    return svc


def _uploaded_bytes(svc, call="update"):
    method = getattr(svc.files.return_value, call)
    stream = method.call_args.kwargs["media_body"]._fd
    stream.seek(0)
    return stream.read()


@pytest.fixture(autouse=True)
def _env_and_resolution(monkeypatch):
    monkeypatch.setenv(mbt.ROOTS_ENV, ROOTS)
    with patch.object(mbt, "resolve_drive_item", new=AsyncMock(side_effect=lambda s, i, **k: (i, {}))), \
         patch.object(mbt, "_ancestors", new=AsyncMock(return_value=["f1", "sub", "OPS_ROOT", "DRIVE"])):
        yield


# --- write path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_replace_keeps_same_file_id_and_exact_bytes():
    svc = _service()
    out = await mbt._write_drive_text_file_impl(svc, "f1", "# New\n", "replace", MT)
    kwargs = svc.files.return_value.update.call_args.kwargs
    assert kwargs["fileId"] == "f1" and kwargs["supportsAllDrives"] is True
    assert _uploaded_bytes(svc) == b"# New\n"
    assert "same file ID f1" in out and "Tier: operations" in out
    svc.files.return_value.create.assert_not_called()


@pytest.mark.asyncio
async def test_append_adds_newline_seam():
    svc = _service()
    with patch.object(mbt, "_download_text", new=AsyncMock(return_value="line one")):
        await mbt._write_drive_text_file_impl(svc, "f1", "line two\n", "append")
    assert _uploaded_bytes(svc) == b"line one\nline two\n"


@pytest.mark.asyncio
async def test_append_no_double_newline():
    svc = _service()
    with patch.object(mbt, "_download_text", new=AsyncMock(return_value="line one\n")):
        await mbt._write_drive_text_file_impl(svc, "f1", "line two\n", "append")
    assert _uploaded_bytes(svc) == b"line one\nline two\n"


@pytest.mark.asyncio
async def test_conflict_refuses_write():
    svc = _service()
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", "2026-09-30T00:00:00.000Z")
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_refuses_google_native_files():
    svc = _service(state={**STATE, "mimeType": "application/vnd.google-apps.document"})
    with pytest.raises(ValueError, match="Google-native"):
        await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_bad_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        await mbt._write_drive_text_file_impl(_service(), "f1", "x", "overwrite")


@pytest.mark.asyncio
async def test_utf8_round_trip():
    svc = _service()
    text = "Ben’s note: 50% “quoted” café\n"
    await mbt._write_drive_text_file_impl(svc, "f1", text, "replace", MT)
    assert _uploaded_bytes(svc).decode("utf-8") == text


# --- fence ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fence_refuses_outside_approved_roots():
    svc = _service()
    with patch.object(mbt, "_ancestors", new=AsyncMock(return_value=["f1", "OTHER", "DRIVE"])):
        with pytest.raises(PermissionError, match="not inside an approved Brain folder"):
            await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_fence_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv(mbt.ROOTS_ENV, raising=False)
    svc = _service()
    with pytest.raises(PermissionError, match="not configured"):
        await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_tier_mismatch_refused_on_replace():
    svc = _service()
    page = "---\nname: x\ntier: company\n---\n# x\n"
    with pytest.raises(PermissionError, match="declares tier 'company'"):
        await mbt._write_drive_text_file_impl(svc, "f1", page, "replace", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_matching_tier_allowed():
    svc = _service()
    page = "---\nname: x\ntier: operations\n---\n# x\n"
    await mbt._write_drive_text_file_impl(svc, "f1", page, "replace", MT)
    svc.files.return_value.update.assert_called_once()


@pytest.mark.asyncio
async def test_create_brain_file_fenced_and_tier_checked():
    svc = _service()
    out = await mbt._create_brain_file_impl(svc, "sub", "p.md", "---\ntier: operations\n---\nhi\n")
    assert "tier operations" in out
    assert _uploaded_bytes(svc, "create") == b"---\ntier: operations\n---\nhi\n"
    with patch.object(mbt, "_ancestors", new=AsyncMock(return_value=["x", "OTHER"])):
        with pytest.raises(PermissionError):
            await mbt._create_brain_file_impl(svc, "x", "p.md", "hi")


@pytest.mark.asyncio
async def test_create_brain_file_rejects_non_text_mime():
    with pytest.raises(ValueError, match="mime_type"):
        await mbt._create_brain_file_impl(_service(), "sub", "p.md", "hi", "application/pdf")


# --- collision, pin, restore -------------------------------------------------

@pytest.mark.asyncio
async def test_collision_detected_when_other_revision_lands_between():
    svc = _service(revisions=[{"id": "r3"}, {"id": "rX"}, {"id": "r4"}])
    out = await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    assert "COLLISION" in out and "rX" in out


@pytest.mark.asyncio
async def test_no_collision_when_our_write_follows_expected_head():
    out = await mbt._write_drive_text_file_impl(_service(), "f1", "x", "replace", MT)
    assert "COLLISION" not in out


@pytest.mark.asyncio
async def test_new_revision_is_pinned_by_default():
    svc = _service()
    out = await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    kwargs = svc.revisions.return_value.update.call_args.kwargs
    assert kwargs["revisionId"] == "r4" and kwargs["body"] == {"keepForever": True}
    assert "Pinned revision r4" in out


@pytest.mark.asyncio
async def test_restore_writes_old_revision_content_as_new_head():
    svc = _service()
    with patch.object(mbt, "_download", new=AsyncMock(return_value=b"old content\n")):
        out = await mbt._restore_drive_file_revision_impl(svc, "f1", "r2", MT)
    assert _uploaded_bytes(svc) == b"old content\n"
    assert "Restored content of revision r2" in out


def test_state_format_includes_editor_and_hash():
    out = mbt._format_state(STATE)
    assert "ben@example.com" in out and "md5Checksum: abc" in out


@pytest.mark.asyncio
async def test_revisions_list_shows_each_editor():
    svc = _service(revisions=[
        {"id": "r1", "lastModifyingUser": {"emailAddress": "alex@example.com"}},
        {"id": "r2", "lastModifyingUser": {"emailAddress": "ben@example.com"}},
    ])
    out = await mbt._list_drive_file_revisions_impl(svc, "f1")
    assert "alex@example.com" in out and "ben@example.com" in out


# --- scope switch --------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]),
        ("drive.file", ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]),
        ("drive", ["https://www.googleapis.com/auth/drive"]),
        ("readonly", ["https://www.googleapis.com/auth/drive.readonly"]),
    ],
)
def test_drive_scope_switch(monkeypatch, value, expected):
    from auth import scopes

    if value is None:
        monkeypatch.delenv("MICADOR_DRIVE_WRITE_SCOPE", raising=False)
    else:
        monkeypatch.setenv("MICADOR_DRIVE_WRITE_SCOPE", value)
    assert scopes._micador_drive_scopes() == expected


@pytest.mark.asyncio
async def test_unfenced_mode_allows_any_folder_and_skips_tier(monkeypatch):
    monkeypatch.setenv(mbt.ROOTS_ENV, "*")
    svc = _service()
    with patch.object(mbt, "_ancestors", new=AsyncMock(return_value=["f1", "ANYWHERE"])):
        out = await mbt._write_drive_text_file_impl(svc, "f1", "---\ntier: company\n---\nx\n", "replace", MT)
    assert "Tier: any" in out
    svc.files.return_value.update.assert_called_once()


# --- v4 fixes (Astra review, 23 Sep 2026) --------------------------------------

@pytest.mark.asyncio
async def test_replace_without_precondition_is_refused():
    """Astra finding 2: a replace built from an older read must not silently overwrite."""
    svc = _service()
    with pytest.raises(ValueError, match="expected_modified_time is required"):
        await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace")
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_stale_read_then_other_edit_is_conflict():
    """Ben read at T0; another person saved T1; Ben's replace carrying T0 is refused."""
    svc = _service(state={**STATE, "modifiedTime": "2026-10-01T09:00:00.000Z", "headRevisionId": "rOther"})
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await mbt._write_drive_text_file_impl(svc, "f1", "Ben's old-based text", "replace", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_append_allowed_without_precondition():
    svc = _service()
    with patch.object(mbt, "_download_text", new=AsyncMock(return_value="a\n")):
        await mbt._write_drive_text_file_impl(svc, "f1", "b\n", "append")
    svc.files.return_value.update.assert_called_once()


@pytest.mark.asyncio
async def test_pdf_in_brain_folder_refused_on_replace():
    """Astra finding 3: an existing PDF must not be overwritten with text."""
    svc = _service(state={**STATE, "name": "agreement.pdf", "mimeType": "application/pdf"})
    with pytest.raises(ValueError, match="application/pdf"):
        await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_in_brain_folder_refused_on_append():
    svc = _service(state={**STATE, "mimeType": "application/pdf"})
    with pytest.raises(ValueError, match="application/pdf"):
        await mbt._write_drive_text_file_impl(svc, "f1", "x", "append")
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_refused_on_restore():
    svc = _service(state={**STATE, "mimeType": "application/pdf"})
    with pytest.raises(ValueError, match="Refusing to restore"):
        await mbt._restore_drive_file_revision_impl(svc, "f1", "r2", MT)
    svc.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_restore_requires_precondition():
    with pytest.raises(ValueError, match="required for restore"):
        await mbt._restore_drive_file_revision_impl(_service(), "f1", "r2")


@pytest.mark.asyncio
async def test_x_markdown_mime_is_writable():
    svc = _service(state={**STATE, "mimeType": "text/x-markdown"})
    await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    svc.files.return_value.update.assert_called_once()


@pytest.mark.asyncio
async def test_revision_listing_follows_next_page_token():
    """Astra finding 4: every page of revisions is read."""
    svc = MagicMock()
    svc.revisions.return_value.list.return_value.execute = MagicMock(
        side_effect=[
            {"revisions": [{"id": "r1"}], "nextPageToken": "p2"},
            {"revisions": [{"id": "r2"}], "nextPageToken": "p3"},
            {"revisions": [{"id": "r3"}]},
        ]
    )
    revs = await mbt._list_revisions(svc, "f1")
    assert [r["id"] for r in revs] == ["r1", "r2", "r3"]
    tokens = [c.kwargs.get("pageToken") for c in svc.revisions.return_value.list.call_args_list]
    assert tokens == [None, "p2", "p3"]


@pytest.mark.asyncio
async def test_collision_check_incomplete_when_new_revision_not_found():
    svc = _service(revisions=[{"id": "r1"}, {"id": "r3"}])  # r4 missing
    out = await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    assert "COLLISION CHECK INCOMPLETE" in out


@pytest.mark.asyncio
async def test_collision_check_incomplete_when_revisions_unreadable():
    svc = _service()
    svc.revisions.return_value.list.return_value.execute = MagicMock(side_effect=RuntimeError("boom"))
    out = await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    assert "COLLISION CHECK INCOMPLETE" in out and "boom" in out


@pytest.mark.asyncio
async def test_write_succeeds_but_pin_fails_is_reported():
    """Interrupted-write case: page written, retention step failed. Must be visible."""
    svc = _service()
    svc.revisions.return_value.update.return_value.execute = MagicMock(side_effect=RuntimeError("quota"))
    out = await mbt._write_drive_text_file_impl(svc, "f1", "x", "replace", MT)
    svc.files.return_value.update.assert_called_once()
    assert "WARNING: could not pin revision r4" in out
