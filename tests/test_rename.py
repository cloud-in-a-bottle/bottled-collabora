import re
import time

import requests
from openhost_test_harness import OpenhostStack
from playwright.sync_api import Page
from playwright.sync_api import expect


def _logs(stack: OpenhostStack) -> str:
    return f"\n\napp logs:\n{stack.app_logs()[-5000:]}"


def _new_document(stack: OpenhostStack) -> str:
    # Retry while the app is still starting.
    deadline = time.monotonic() + 60
    while True:
        created = stack.owner_session.post(f"{stack.url}/new/writer", allow_redirects=False, timeout=30)
        if created.status_code in (301, 302, 303) or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert created.status_code in (301, 302, 303), f"new document: {created.status_code}: {created.text[:300]}"
    match = re.search(r"/open/([0-9a-f]{32})", created.headers["Location"])
    assert match, f"unexpected redirect {created.headers['Location']}"
    return match.group(1)


def _owner_wopi_token(stack: OpenhostStack, file_id: str) -> str:
    page = stack.owner_session.get(f"{stack.url}/open/{file_id}", timeout=30)
    match = re.search(r'name="access_token" value="([^"]+)"', page.text)
    assert match, f"no access_token on the editor page: {page.text[:500]}"
    return match.group(1)


def _file_names(stack: OpenhostStack) -> list[str]:
    return re.findall(
        r'<a class="filename" href="/open/[0-9a-f]{32}">([^<]*)</a>', stack.owner_session.get(stack.url).text
    )


def _wopi_logs(stack: OpenhostStack) -> str:
    lines = [line for line in stack.app_logs().splitlines() if re.search(r"wopi|rename", line, re.IGNORECASE)]
    return "\n".join(lines)[-6000:]


def test_wopi_rename(stack: OpenhostStack) -> None:
    file_id = _new_document(stack)
    token = _owner_wopi_token(stack, file_id)
    # What coolwsd sends: the new name without its extension, UTF-7 encoded ("Café notes").
    response = requests.post(
        f"{stack.url}/wopi/files/{file_id}?access_token={token}",
        headers={"X-WOPI-Override": "RENAME_FILE", "X-WOPI-RequestedName": "Caf+AOk- notes"},
        timeout=30,
    )
    assert response.status_code == 200, f"RENAME_FILE: {response.status_code}: {response.text[:300]}{_logs(stack)}"
    body = response.json()
    # The editor only applies a rename (header, title, WOPISrc) when the response carries both the name and the URL.
    assert body.get("Name") == "Café notes.odt", f"RENAME_FILE response: {body}"
    assert str(body.get("Url", "")).split("?")[0].endswith(f"/wopi/files/{file_id}"), f"RENAME_FILE response: {body}"

    info = requests.get(f"{stack.url}/wopi/files/{file_id}?access_token={token}", timeout=30).json()
    assert info["BaseFileName"] == "Café notes.odt", f"CheckFileInfo after rename: {info}"
    assert "Café notes.odt" in _file_names(stack)


def test_share_recipients_cannot_rename(stack: OpenhostStack) -> None:
    file_id = _new_document(stack)
    manage = stack.owner_session.post(f"{stack.url}/manage/{file_id}/create", data={"mode": "edit"}, timeout=30)
    tokens = re.findall(r'value="[^"]*/share/e/([A-Za-z0-9_-]{32})"', manage.text)
    assert len(tokens) == 1, f"expected one edit share URL on the manage page: {manage.text[:2000]}"
    url = f"{stack.url}/wopi/files/{file_id}?access_token={tokens[0]}"
    assert requests.get(url, timeout=30).json()["UserCanRename"] is False
    response = requests.post(
        url, headers={"X-WOPI-Override": "RENAME_FILE", "X-WOPI-RequestedName": "guest"}, timeout=30
    )
    assert response.status_code == 403, f"share-recipient RENAME_FILE: {response.status_code}"


def test_rename_from_the_editor_header(stack: OpenhostStack, page: Page) -> None:
    file_id = _new_document(stack)
    stack.playwright_login(page)
    page.goto(f"{stack.url}/open/{file_id}")
    frame = page.frame_locator("#cool-iframe")
    name_input = frame.locator("#document-name-input")
    try:
        expect(name_input).to_be_enabled(timeout=120_000)
        expect(name_input).not_to_have_value("", timeout=30_000)
    except AssertionError as e:
        raise AssertionError(f"the editor's name field never became editable: {e}{_logs(stack)}") from e
    old_name = name_input.input_value()
    # A first-run welcome dialog covers the editor; focus the field directly rather than clicking through it.
    name_input.focus()
    name_input.fill("Renamed doc")
    name_input.press("Enter")

    # Record what the header shows, and the editor's idea of the file name, for a while after Enter.
    cool = page.frame(name="cool-iframe")
    assert cool is not None
    seen: list[tuple[float, str, str]] = []
    start = time.monotonic()
    while time.monotonic() - start < 25:
        try:
            value, base = cool.evaluate(
                "() => [document.getElementById('document-name-input')?.value ?? '',"
                " (window.app && app.map && app.map.wopi && app.map.wopi.BaseFileName) || '']"
            )
        except Exception as e:  # the frame may be mid-reload
            value, base = f"({type(e).__name__})", ""
        if not seen or seen[-1][1:] != (value, base):
            seen.append((round(time.monotonic() - start, 1), value, base))
        time.sleep(0.2)
    history = "\n".join(f"  {t:5.1f}s  header={v!r}  BaseFileName={b!r}" for t, v, b in seen)
    details = f"\nafter Enter:\n{history}\n\nWOPI log:\n{_wopi_logs(stack)}"

    renamed = [t for t, v, b in seen if b == "Renamed doc.odt"]
    assert renamed and renamed[0] <= 3, f"the editor didn't take the new name promptly{details}"
    first = renamed[0]
    assert not any(t >= first and (v == old_name or b == old_name) for t, v, b in seen), (
        f"the header went back to the old name {old_name!r} after the rename{details}"
    )
    assert seen[-1][1] == "Renamed doc.odt", f"the header doesn't show the new name{details}"
    assert "Renamed doc.odt" in _file_names(stack), f"file list: {_file_names(stack)}{details}"
