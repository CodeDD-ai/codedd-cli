"""
General-purpose payload inspection for the ``--show`` flag.

Any CLI command that sends data to CodeDD can use this module to:
    1. Write the outgoing payload or request to a human-readable ``.txt`` file.
    2. Open the file for the user to review.
    3. Ask for explicit confirmation before sending.

This ensures full transparency: users always know **exactly** what
data leaves their machine.

Use :func:`review_payload` for POST bodies (e.g. scope registration).
Use :func:`review_request` for GET/POST request summaries (method, endpoint, params, body).

Usage example::

    from codedd_cli.utils.payload_inspector import review_payload, review_request

    # POST with JSON body
    if not review_payload(payload, command_label="Scope Registration"):
        raise typer.Exit()

    # GET or request summary
    if not review_request("GET", "/api/cli/scope/files/", params={"audit_uuid": u}):
        raise typer.Exit()
"""

import json
import os
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

from rich.console import Console
from rich.prompt import Confirm

console = Console()


def write_payload_file(
    payload: Any,
    *,
    command_label: str = "API Request",
    context_note: str = "This file contains ONLY metadata. No source code content is included.",
) -> str:
    """
    Serialise *payload* to a formatted ``.txt`` file in the system temp directory.

    The file includes a human-readable header, the context note, and the
    full JSON payload.

    Args:
        payload:       Any JSON-serialisable object (dict, list, etc.).
        command_label: Short label shown in the file header (e.g. ``"Scope Registration"``).
        context_note:  Explanatory text placed before the JSON body.

    Returns:
        Absolute path to the written file.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in command_label)
    filename = f"codedd_{safe_label}_{timestamp}.txt"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write("=" * 72 + "\n")
        fh.write(f"  CodeDD CLI — {command_label}\n")
        fh.write(f"  Generated: {datetime.now(timezone.utc).isoformat()}\n")
        fh.write("=" * 72 + "\n\n")
        if context_note:
            fh.write(context_note + "\n\n")
        fh.write("-" * 72 + "\n\n")
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")

    return filepath


def open_file(filepath: str) -> None:
    """
    Attempt to open *filepath* with the operating system's default viewer.

    Fails silently — the path is always printed separately so the user
    can open it manually.
    """
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(filepath)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", filepath], check=False)
        else:
            subprocess.run(["xdg-open", filepath], check=False)
    except Exception:
        pass


def review_payload(
    payload: Any,
    *,
    command_label: str = "API Request",
    context_note: str = "This file contains ONLY metadata. No source code content is included.",
    confirm_prompt: str = "Proceed with sending this data to CodeDD?",
) -> bool:
    """
    Write the payload to a file, open it, and ask the user for confirmation.

    This is the **one-call** convenience function that combines
    :func:`write_payload_file`, :func:`open_file`, and a ``Confirm`` prompt.

    Args:
        payload:        JSON-serialisable data.
        command_label:  Label for the file header.
        context_note:   Transparency note shown in the file.
        confirm_prompt: Question text for the confirmation prompt.

    Returns:
        ``True`` if the user confirmed, ``False`` if they declined.
    """
    filepath = write_payload_file(
        payload,
        command_label=command_label,
        context_note=context_note,
    )

    console.print(f"\n[bold]Payload written to:[/bold] {filepath}\n")
    console.print(
        f"[dim]Review the file above.  {context_note}[/dim]"
    )
    open_file(filepath)
    console.print()

    return Confirm.ask(confirm_prompt, default=True)


def review_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[Any] = None,
    command_label: str = "API Request",
    context_note: str = "This file describes the request that will be sent to CodeDD.",
    confirm_prompt: str = "Proceed with sending this request to CodeDD?",
) -> bool:
    """
    Build a request summary (method, path, params, body) and run the review flow.

    Use for GET requests (params only), POST with body, or any call where you
    want a single transparency file showing exactly what will be sent.

    Args:
        method: HTTP method (e.g. "GET", "POST").
        path: API path (e.g. "/api/cli/scope/files/").
        params: Optional query parameters (for GET or POST).
        json_body: Optional JSON body (for POST/PUT).
        command_label: Label for the file header.
        context_note: Transparency note in the file.
        confirm_prompt: Confirmation question.

    Returns:
        True if the user confirmed, False if they declined.
    """
    payload: dict[str, Any] = {
        "method": method.upper(),
        "endpoint": path,
    }
    if params:
        payload["query_params"] = params
    if json_body is not None:
        payload["body"] = json_body

    return review_payload(
        payload,
        command_label=command_label,
        context_note=context_note,
        confirm_prompt=confirm_prompt,
    )
