"""MICADOR: guard the Drive tool surface at the live tier (TOOL_TIER=extended)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.tool_tier_loader import resolve_tools_from_tier  # noqa: E402

LIVE_SERVICES = "chat sheets slides forms tasks search contacts drive".split()

MUST_STAY_PARKED = {
    "manage_drive_access",
    "set_drive_file_permissions",
    "update_drive_file",
    "create_drive_file",
    "create_drive_folder",
    "copy_drive_file",
    "get_drive_shareable_link",
    "import_to_google_doc",
}

BRAIN_TOOLS = {
    "create_brain_file",
    "create_brain_folder",
    "write_drive_text_file",
    "restore_drive_file_revision",
    "pin_drive_file_revision",
    "get_drive_file_state",
    "list_drive_file_revisions",
}


def test_permission_and_generic_write_tools_stay_parked():
    tools, _ = resolve_tools_from_tier("extended", LIVE_SERVICES)
    leaked = MUST_STAY_PARKED & set(tools)
    assert not leaked, f"Parked Drive tools loaded at extended tier: {sorted(leaked)}"


def test_brain_tools_load_at_live_tier():
    tools, _ = resolve_tools_from_tier("extended", LIVE_SERVICES)
    missing = BRAIN_TOOLS - set(tools)
    assert not missing, f"Brain tools missing at extended tier: {sorted(missing)}"
