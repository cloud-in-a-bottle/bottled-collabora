import time
from typing import Any

import requests
from openhost_test_harness import OpenhostStack

# coolwsd's admin console (pages, their assets, the admin websocket and the metrics endpoint) is reachable through the
# public /browser/ and /cool/ prefixes. No admin username is configured, so nobody can log in, but neither our UI nor
# the editor uses any of it, so the proxy refuses it for everyone.
ADMIN_PATHS = (
    "/browser/dist/admin/admin.html",
    "/browser/dist/admin/adminSettings.html",
    "/browser/dist/admin/adminAnalytics.html",
    "/browser/dist/admin/css/admin.css",
    "/browser/dist/admin-bundle.js",
    "/browser/dist/adminIntegratorSettings.html",
    "/browser/dist/fetch-settings-config",
    "/cool/getMetrics",
)


def _get(stack: OpenhostStack, path: str, **kwargs: Any) -> requests.Response:
    # Retry while coolwsd is still starting (the proxy answers 502/504 until it's up).
    deadline = time.monotonic() + 90
    while True:
        try:
            response = requests.get(f"{stack.url}{path}", allow_redirects=False, timeout=30, **kwargs)
            if response.status_code not in (502, 503, 504) or time.monotonic() > deadline:
                return response
        except requests.ConnectionError:
            if time.monotonic() > deadline:
                raise
        time.sleep(3)


def test_anonymous_admin_console_is_refused(stack: OpenhostStack) -> None:
    statuses = {path: _get(stack, path).status_code for path in ADMIN_PATHS}
    assert all(status == 403 for status in statuses.values()), (
        f"anonymous admin console paths not refused: {statuses}\n\napp logs:\n{stack.app_logs()[-5000:]}"
    )
    # The editor's own assets under /browser/ must still load.
    response = _get(stack, "/browser/dist/bundle.js")
    assert response.status_code == 200, f"editor bundle: {response.status_code}: {response.content[:200]!r}"


def test_anonymous_admin_websocket_is_refused(stack: OpenhostStack) -> None:
    headers = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version": "13",
    }
    for path in ("/cool/adminws", "/lool/adminws"):
        response = _get(stack, path, headers=headers, stream=True)
        try:
            assert response.status_code != 101, (
                f"anonymous websocket {path} was upgraded\n\napp logs:\n{stack.app_logs()[-5000:]}"
            )
        finally:
            response.close()
