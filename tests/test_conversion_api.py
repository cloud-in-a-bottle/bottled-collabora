import time

import requests
from openhost_test_harness import OpenhostStack

# coolwsd's conversion API runs LibreOffice on whatever is uploaded (GHSA-cf9v-hrj7-8p4v: it can run macros). coolwsd
# only checks that the caller is on loopback or a private network, which our proxy always is, and /cool/ is public, so
# the proxy refuses these paths for everyone. Neither our UI nor the editor uses them.
UPLOAD = {"data": ("note.txt", b"hello\n", "text/plain")}


def _post(stack: OpenhostStack, path: str) -> requests.Response:
    # Retry while coolwsd is still starting (the proxy answers 502/504 until it's up).
    deadline = time.monotonic() + 90
    while True:
        try:
            response = requests.post(f"{stack.url}{path}", files=UPLOAD, allow_redirects=False, timeout=60)
            if response.status_code not in (502, 503, 504) or time.monotonic() > deadline:
                return response
        except requests.ConnectionError:
            if time.monotonic() > deadline:
                raise
        time.sleep(3)


def test_anonymous_convert_to_is_refused(stack: OpenhostStack) -> None:
    response = _post(stack, "/cool/convert-to/pdf")
    assert response.status_code == 403 and not response.content.startswith(b"%PDF"), (
        f"anonymous convert-to: {response.status_code} {response.headers.get('Content-Type')}: "
        f"{response.content[:200]!r}\n\napp logs:\n{stack.app_logs()[-5000:]}"
    )
    # Spellings coolwsd treats the same (or that a less careful block would miss) must not convert either.
    for path in (
        "/lool/convert-to/pdf",
        "/cool/convert-to?format=pdf",
        "//cool//convert-to/pdf",
        "/cool/convert%2Dto/pdf",
        "/cool/?convert-to/pdf",
        "/cool/get-thumbnail",
        "/cool/extract-document-structure",
    ):
        response = _post(stack, path)
        assert response.status_code != 200 and not response.content.startswith(b"%PDF"), (
            f"anonymous {path}: {response.status_code}: {response.content[:200]!r}"
        )
