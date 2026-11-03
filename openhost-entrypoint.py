"""Two-process supervisor for the Collabora-with-UI image.

Children:
  1. coolwsd  (Collabora's editor backend)       -- listens 127.0.0.1:9980
  2. hypercorn -> Quart UI + WOPI host + proxy    -- listens 0.0.0.0:8080

The collabora/code image has no shell since 26.04.2.2, so this is Python (run by the interpreter we copy into the
image) rather than bash.  It derives coolwsd's config from the zone, starts both children, forwards SIGTERM/SIGINT to
them, and exits when either one dies; there is no restart logic, Cloud in a Bottle restarts the container.  As PID 1
it also reaps any orphaned grandchildren.

coolwsd is launched exactly as the upstream image's ENTRYPOINT does (``coolwsd --use-env-vars`` plus the same
``--o:`` paths), with our settings appended as further ``--o:`` arguments.
"""

import os
import signal
import subprocess
import sys

# The upstream image's ENTRYPOINT, verbatim (docker.io/collabora/code 26.04).
COOLWSD_UPSTREAM_ARGV = [
    "/usr/bin/coolwsd",
    "--use-env-vars",
    "--o:sys_template_path=/opt/cool/systemplate",
    "--o:child_root_path=/opt/cool/child-roots",
    "--o:file_server_root_path=/usr/share/coolwsd",
    "--o:cache_files.path=/opt/cool/cache",
    "--o:logging.color=false",
    "--o:stop_on_config_change=true",
]

HYPERCORN_ARGV = [
    "/opt/openhost-venv/bin/hypercorn",
    "--bind",
    "0.0.0.0:8080",
    "--access-logfile",
    "-",
    "--error-logfile",
    "-",
    "--workers",
    "1",
    "server:app",
]


def _is_dev_zone(zone_domain: str) -> bool:
    # Real zones are served over https (TLS terminated in front of the router).  Dev zones (a dev router or the test
    # harness) are plain http, and their zone domain may carry the router's port (eg harness.localhost:59285), so
    # match on the hostname alone.  server.py builds the browser-facing URLs from the scheme too.
    host = zone_domain.split(":", 1)[0]
    return host in ("lvh.me", "localhost") or host.endswith((".lvh.me", ".localhost"))


def coolwsd_argv(zone_domain: str, app_name: str, public_scheme: str, wopi_host_regex: str) -> list[str]:
    ssl_termination = "true" if public_scheme == "https" else "false"
    return [
        *COOLWSD_UPSTREAM_ARGV,
        # What coolwsd publishes in /hosting/discovery URLs; it has to match what the user's browser sees, since
        # that's the URL the editor iframe connects back to.
        f"--o:server_name={app_name}.{zone_domain}",
        # Plain HTTP between Quart and coolwsd (loopback) and between the router and Quart.
        "--o:ssl.enable=false",
        # true on real zones: coolwsd builds outbound URLs (and the editor's wss:// socket) as https, reflecting what
        # the public-facing TLS terminator serves.  false on plain-http dev zones.
        f"--o:ssl.termination={ssl_termination}",
        # Loopback so coolwsd can only be reached through Quart's proxy (which blocks the conversion API).
        "--o:net.proto=IPv4",
        "--o:net.listen=loopback",
        # WOPI hosts permitted to load documents: our own WOPI host on loopback (the WOPISrc server.py hands out),
        # plus any host in the zone (or WOPI_HOST_REGEX).
        "--o:storage.wopi.alias_groups[@mode]=groups",
        "--o:storage.wopi.alias_groups.group[0].host[@allow]=true",
        r"--o:storage.wopi.alias_groups.group[0].host=http://127\.0\.0\.1:8080",
        "--o:storage.wopi.alias_groups.group[1].host[@allow]=true",
        f"--o:storage.wopi.alias_groups.group[1].host={wopi_host_regex}",
        "--o:logging.level=warning",
    ]


def main() -> int:
    zone_domain = os.environ.get("OPENHOST_ZONE_DOMAIN", "localhost")
    app_name = os.environ.get("OPENHOST_APP_NAME", "collabora")
    public_scheme = "http" if _is_dev_zone(zone_domain) else "https"
    escaped_zone = zone_domain.replace(".", r"\.")
    # coolwsd parses each alias_groups host as a URI (only its host and port are matched, as a regex), so the
    # default can't use a bracket expression like [^/]: Poco reads "[" as the start of an IPv6 address.
    wopi_host_regex = os.environ.get("WOPI_HOST_REGEX", rf"{public_scheme}://.+\.{escaped_zone}")

    env = dict(os.environ, COLLABORA_PUBLIC_SCHEME=public_scheme)
    # Skips coolwsd's self-signed certificate generation; TLS is terminated in front of the router.
    env["DONT_GEN_SSL_CERT"] = "1"

    coolwsd = subprocess.Popen(coolwsd_argv(zone_domain, app_name, public_scheme, wopi_host_regex), env=env)
    print(f"[entrypoint] coolwsd pid={coolwsd.pid}", flush=True)
    hypercorn = subprocess.Popen(HYPERCORN_ARGV, env=env, cwd="/opt/openhost-app")
    print(f"[entrypoint] quart pid={hypercorn.pid}", flush=True)
    children = {coolwsd.pid: "coolwsd", hypercorn.pid: "quart"}

    def stop_children() -> None:
        for pid in children:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    # A stop signal is a clean shutdown (exit 0); a child dying on its own is a failure (exit 1).
    exit_code = 1

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal exit_code
        print(f"[entrypoint] signal {signum} received, stopping children", flush=True)
        exit_code = 0
        stop_children()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # Wait for any child (reaping orphans too, as PID 1).  When one of ours exits, stop the other and wait for it.
    while children:
        pid, status = os.wait()
        name = children.pop(pid, None)
        if name is not None:
            print(f"[entrypoint] {name} exited (code {os.waitstatus_to_exitcode(status)})", flush=True)
            stop_children()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
