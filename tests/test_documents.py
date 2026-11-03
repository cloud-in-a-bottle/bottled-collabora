import re
import time

import requests
from openhost_test_harness import OpenhostStack
from playwright.sync_api import Page

CONTENT = b"hello from the upstream-coverage test\n"


def _logs(stack: OpenhostStack) -> str:
    return f"\n\napp logs:\n{stack.app_logs()[-5000:]}"


def _wait_for_ui(stack: OpenhostStack) -> None:
    deadline = time.monotonic() + 60
    while True:
        response = stack.owner_session.get(f"{stack.url}/", headers={"Accept": "text/html"}, timeout=30)
        if response.status_code == 200 or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert response.status_code == 200, f"GET / returned {response.status_code}: {response.text[:500]}{_logs(stack)}"


def _upload(stack: OpenhostStack, name: str) -> str:
    _wait_for_ui(stack)
    response = stack.owner_session.post(f"{stack.url}/upload", files={"file": (name, CONTENT)}, timeout=30)
    assert response.status_code == 200, f"upload: {response.status_code}: {response.text[:500]}"
    ids = re.findall(rf'<a class="filename" href="/open/([0-9a-f]{{32}})">{re.escape(name)}</a>', response.text)
    assert ids, f"{name} not in the file list after upload: {response.text[:2000]}"
    return ids[0]


def test_upload_download_and_storage(stack: OpenhostStack) -> None:
    file_id = _upload(stack, "stored.txt")
    response = stack.owner_session.get(f"{stack.url}/download/{file_id}", timeout=30)
    assert response.status_code == 200 and response.content == CONTENT, f"download: {response.status_code}"
    assert (stack.data_dir / "files" / file_id).read_bytes() == CONTENT, "upload not stored under app_data/files"
    assert (stack.data_dir / "index.db").is_file()
    # The owner routes stay behind the router login.
    anonymous = requests.get(f"{stack.url}/download/{file_id}", allow_redirects=False, timeout=30)
    assert anonymous.status_code in (302, 303, 307, 401), f"anonymous download: {anonymous.status_code}"
    assert anonymous.content != CONTENT


def test_download_share_link_works_anonymously(stack: OpenhostStack) -> None:
    file_id = _upload(stack, "shared.txt")
    manage = stack.owner_session.post(f"{stack.url}/manage/{file_id}/create", data={"mode": "download"}, timeout=30)
    assert manage.status_code == 200, f"create share: {manage.status_code}: {manage.text[:500]}"
    urls = re.findall(r'value="([^"]*/share/d/[A-Za-z0-9_-]{32})"', manage.text)
    assert len(urls) == 1, f"expected one download share URL on the manage page: {manage.text[:2000]}"
    share_url = urls[0]
    # The link the owner copies must be the app's public URL.
    assert share_url.startswith(f"{stack.url}/share/d/"), f"share URL {share_url} isn't under {stack.url}"

    response = requests.get(share_url, timeout=30)
    assert response.status_code == 200 and response.content == CONTENT, (
        f"anonymous share download: {response.status_code}: {response.text[:300]}"
    )

    token = share_url.rsplit("/", 1)[1]
    revoke = stack.owner_session.post(f"{stack.url}/manage/{file_id}/revoke", data={"token": token}, timeout=30)
    assert revoke.status_code == 200, f"revoke: {revoke.status_code}"
    assert requests.get(share_url, timeout=30).status_code == 404, "revoked share link still works"


def test_new_document_opens_in_editor(stack: OpenhostStack, page: Page) -> None:
    _wait_for_ui(stack)
    created = stack.owner_session.post(f"{stack.url}/new/writer", allow_redirects=False, timeout=30)
    assert created.status_code in (301, 302, 303), f"new document: {created.status_code}: {created.text[:300]}"
    match = re.search(r"/open/([0-9a-f]{32})", created.headers["Location"])
    assert match, f"unexpected redirect {created.headers['Location']}"
    file_id = match.group(1)

    stack.playwright_login(page)
    page.goto(f"{stack.url}/open/{file_id}")
    # The browser loads coolwsd's editor through our proxy, which opens the websocket, and coolwsd then fetches the
    # document from our WOPI host. That fetch only happens once the whole chain works.
    wopi_fetch = re.compile(rf'"GET /wopi/files/{file_id}/contents\S* [^"]*" 200')
    deadline = time.monotonic() + 120
    logs = ""
    while time.monotonic() < deadline:
        logs = stack.app_logs()
        if wopi_fetch.search(logs):
            # CODE 26.04's editor connects on the "compact" URL (/cool/ws?WOPISrc=...) and only falls back to the
            # legacy one (/cool/<WOPISrc>/ws) if that fails, so check the compact one went through our proxy.
            # hypercorn's access log leaves out the query string.
            assert re.search(r'"GET /cool/ws(\?\S*)? [^"]*" 101', logs), (
                "the editor's websocket didn't connect on the compact URL /cool/ws. Proxied /cool/ requests:\n"
                + "\n".join(line for line in logs.splitlines() if '"GET /cool/' in line)[-3000:]
            )
            return
        time.sleep(3)
    frame = page.frame(name="cool-iframe")
    frame_text = frame.content()[:2000] if frame else "(no editor iframe)"
    raise AssertionError(
        f"coolwsd never fetched the document from the WOPI host.\n\neditor frame:\n{frame_text}\n\nlogs:\n{logs[-5000:]}"
    )
