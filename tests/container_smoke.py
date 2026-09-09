"""Exercise the built image with disposable state, fake data, and blocked account hosts.

Run: .venv/bin/python tests/container_smoke.py
Requires Docker and the project's Playwright environment. Never mounts host auth.
"""

import base64
import hashlib
import json
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "discord-options-relay:local"


def docker(*args, stdin=None, check=True):
    result = subprocess.run(["docker", *args], input=stdin, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[:1500]}")
    return result.stdout.strip()


def main():
    name = "relay-smoke-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    authorization = "Basic " + base64.b64encode(f"relay:{password}".encode()).decode()
    report = {"fixture": "Synthetic PAPER data only; account hosts redirected to loopback; no account login or broker calls", "checks": []}
    checks = report["checks"]

    try:
        docker("network", "create", name)
        docker("volume", "create", name)
        docker("run", "-d", "--name", name, "--init", "--network", name,
               "--add-host", "discord.com:127.0.0.1", "--add-host", "auth.openai.com:127.0.0.1",
               "--add-host", "chatgpt.com:127.0.0.1", "--add-host", "agentic.robinhood.com:127.0.0.1",
               "--shm-size", "1g", "-p", "127.0.0.1::8080", "-v", f"{name}:/data",
               "-e", f"DASHBOARD_PASSWORD={password}", "-e", "CODEX_HOME=/data/codex",
               "-e", "DISPLAY=:99", IMAGE)
        port = int(docker("port", name, "8080/tcp").rsplit(":", 1)[1])
        origin = f"http://127.0.0.1:{port}"

        def request(path, authenticated=True, payload=None, headers=None):
            req = urllib.request.Request(origin + path, data=None if payload is None else json.dumps(payload).encode())
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            if authenticated:
                req.add_header("Authorization", authorization)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        def ready():
            last = "no response"
            for _ in range(40):
                try:
                    code, body = request("/healthz", False)
                    last = f"HTTP {code}: {body[:120]!r}"
                    if code == 200:
                        return
                except (OSError, urllib.error.URLError) as exc:
                    last = str(exc)
                time.sleep(1)
            raise AssertionError(f"Container dashboard did not become healthy: {last}")

        def worker(action):
            docker("exec", name, "supervisorctl", "-c", "/etc/supervisor/conf.d/relay.conf", action, "service")

        def websocket(path, ws_origin, authenticated=True):
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            headers = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket",
                       "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                       "Sec-WebSocket-Protocol: binary", f"Origin: {ws_origin}"]
            if authenticated:
                headers.append("Authorization: " + authorization)
            with socket.create_connection(("127.0.0.1", port), timeout=8) as stream:
                stream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
                data = b""
                while b"\r\n\r\n" not in data:
                    data += stream.recv(4096)
                head, body = data.split(b"\r\n\r\n", 1)
                code = int(head.split()[1])
                if code == 101:
                    while b"RFB " not in body:
                        packet = stream.recv(4096)
                        if not packet:
                            break
                        body += packet
                    assert b"RFB 003." in body, "VNC did not send its RFB greeting"
                return code

        ready()
        worker("stop")
        status = json.loads(request("/api/status")[1])
        assert status["mode"] == "shadow" and status["live_orders_enabled"] is False
        checks.append("fresh volume starts unbound in shadow with live submissions disabled")
        for path in ("/", "/api/status", "/api/setup", "/api/messages", "/browser/vnc.html", "/static/app.js"):
            assert request(path, False)[0] == 401, path
        assert request("/api/status")[0] == 200
        assert request("/browser/vnc.html")[0] == 200
        checks.append("dashboard, API, assets and noVNC require authentication")
        assert request("/api/setup/pause", payload={"paused": True}, headers={"Origin": origin, "Content-Type": "application/json"})[0] == 403
        setup = json.loads(request("/api/setup")[1])
        assert request("/api/setup/pause", payload={"paused": True}, headers={"Origin": "https://untrusted.invalid", "Content-Type": "application/json", "X-Relay-CSRF": setup["csrf_token"]})[0] == 403
        checks.append("setup writes reject missing CSRF and foreign Origin through nginx")
        assert websocket("/browser/websockify", origin) == 101
        assert websocket("/browser/websockify", origin, False) == 401
        assert websocket("/browser/websockify", "https://untrusted.invalid") == 403
        for path in ("/browser/websockify/", "/browser/anything"):
            assert websocket(path, "https://untrusted.invalid") in (400, 403)
        checks.append("authenticated same-origin WebSocket reaches VNC; unauthenticated and alternate-path cross-origin requests denied")
        version = docker("exec", "--user", "relay", name, "codex", "--version")
        assert "0.153.4" in version, version
        report["codex_version"] = version
        docker("exec", name, "python", "-c", "from pathlib import Path; assert not Path('/data/codex/auth.json').exists()")
        docker("exec", "--user", "relay", name, "python", "-c",
               "import subprocess; r=subprocess.run(['codex','login','status'],capture_output=True,text=True); assert 'not logged in' in (r.stdout+r.stderr).lower()")
        assert "--device-auth" in docker("exec", "--user", "relay", name, "codex", "login", "--help")
        checks.append("pinned Linux Codex binary runs; no host authentication imported")
        assert not docker("exec", name, "find", "/app", "-name", "config.local.json", "-o", "-name", "auth.json", "-o", "-name", "*.rtf")
        docker("exec", "--user", "relay", name, "python", "-c",
               "import os,webbrowser; assert os.environ['BROWSER']=='/usr/local/bin/relay-browser'; assert webbrowser.open('about:blank')")
        for _ in range(10):
            processes = docker("exec", name, "ps", "-eo", "args")
            if "--user-data-dir=/data/oauth-browser" in processes:
                break
            time.sleep(.5)
        else:
            raise AssertionError("OAuth browser wrapper did not launch internal Chromium")
        checks.append("OAuth browser helper opens an internal Chromium window using its separate profile")
        docker("cp", str(ROOT / "tests/test_dashboard_ui.py"), f"{name}:/tmp/seed.py")
        docker("exec", "--user", "relay", name, "python", "/tmp/seed.py", "--seed", "/data", "/app/config.example.json")
        checks.append("synthetic messages, interpretations, order and position seeded into container volume")
        hashes = {}
        for relative, route in [("relay/static/index.html", "/"), ("relay/static/app.js", "/static/app.js"), ("relay/static/style.css", "/static/style.css")]:
            served = request(route)[1]
            digest = hashlib.sha256(served).hexdigest()
            assert digest == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), relative
            hashes[relative] = digest
        report["served_sha256"] = hashes
        checks.append("served frontend bytes match current worktree exactly")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(http_credentials={"username": "relay", "password": password}, viewport={"width": 1440, "height": 1100})
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin, wait_until="networkidle")
            page.locator("#messages-shell .message-content").filter(has_text="DEMO: bought SPY").wait_for()
            assert "filled" in page.locator("#orders-shell").inner_text().lower()
            assert "SPY" in page.locator("#positions-shell").inner_text()
            assert page.locator("#messages-shell img").count() == 0
            runtime = json.loads(request("/api/status")[1])["runtime"]
            if runtime["state"] != "running":
                assert "is-healthy" not in page.locator("#sidebar-status-dot").get_attribute("class")
            original = json.loads(docker("exec", name, "cat", "/data/config.json"))
            page.locator("#save-channels").wait_for()
            page.wait_for_function("() => document.querySelector('[data-channel-field=name]').value === 'Demo signals'")
            rows = page.locator(".channel-editor")
            for index, channel in enumerate(original["channels"]):
                rows.nth(index).locator('[data-channel-field="url"]').fill("https://discord.com/channels/111111111111111111/" + channel["id"])
            rows.nth(0).locator('[data-channel-field="name"]').fill("Saved in frontend")
            page.locator("#poll-seconds").fill("3")
            with page.expect_response(lambda response: response.url.endswith("/api/setup/channels") and response.request.method == "POST") as saved_response:
                page.locator("#save-channels").click()
            assert saved_response.value.status == 200, saved_response.value.text()
            saved = json.loads(docker("exec", name, "cat", "/data/config.json"))
            assert saved["channels"][0]["name"] == "Saved in frontend"
            for key in ("mode", "database", "risk", "robinhood"):
                assert saved[key] == original[key], f"Setup changed {key}"
            page.reload(wait_until="networkidle")
            page.wait_for_function("() => document.querySelector('[data-channel-field=name]').value === 'Saved in frontend'")
            page.locator("#pause-relay").click()
            page.wait_for_function("() => document.querySelector('#pause-relay').textContent.includes('Resume')")
            assert json.loads(request("/api/setup")[1])["paused"] is True
            page.locator("#pause-relay").click()
            page.wait_for_function("() => document.querySelector('#pause-relay').textContent.includes('Pause')")
            assert json.loads(request("/api/setup")[1])["paused"] is False
            with page.expect_response(lambda response: response.url.endswith("/api/setup/reconnect") and response.request.method == "POST") as reconnect:
                page.locator("#reconnect-relay").click()
            assert reconnect.value.status == 200
            docker("exec", name, "test", "-f", "/data/state/RECONNECT")
            page.locator("#setup").screenshot(path=str(ROOT / "artifacts/setup-container-preview.png"))
            checks.append("frontend channel save, pause/resume and reconnect reach actual backend and persist without changing risk/mode/binding")
            page.screenshot(path=str(ROOT / "artifacts/dashboard-container-preview.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            vnc = context.new_page()
            vnc.goto(origin + "/browser/vnc.html?autoconnect=true&resize=scale&path=browser/websockify")
            vnc.wait_for_function("() => document.documentElement.classList.contains('noVNC_connected')", timeout=20000)
            browser.close()
        checks.append("actual container UI renders ledger data and mobile layout; noVNC desktop connects")

        cookie_script = '''import json, sys, time
from playwright.sync_api import sync_playwright
c=json.load(open('/data/config.json'))
with sync_playwright() as p:
    browser=p.chromium.launch_persistent_context(c['browser']['profile_dir'], headless=False)
    if sys.argv[1]=='write':
        browser.add_cookies([{'name':'relay_smoke','value':'synthetic','domain':'example.invalid','path':'/','expires':time.time()+3600}])
    else:
        assert any(x['name']=='relay_smoke' and x['value']=='synthetic' for x in browser.cookies()), 'profile cookie missing after restart'
    browser.close()
'''
        docker("exec", "-i", "--user", "relay", name, "python", "-", "write", stdin=cookie_script)
        before = docker("exec", name, "sha256sum", "/data/config.json", "/data/state/demo.sqlite3")
        docker("restart", "--time", "15", name)
        port = int(docker("port", name, "8080/tcp").rsplit(":", 1)[1])
        origin = f"http://127.0.0.1:{port}"
        ready()
        worker("stop")
        after = docker("exec", name, "sha256sum", "/data/config.json", "/data/state/demo.sqlite3")
        assert before == after, "Restart altered existing configuration or ledger"
        docker("exec", "-i", "--user", "relay", name, "python", "-", "read", stdin=cookie_script)
        status = json.loads(request("/api/status")[1])
        assert status["mode"] == "paper" and status["counts"]["messages"] == 3
        assert json.loads(request("/api/setup")[1])["channels"][0]["name"] == "Saved in frontend"
        checks.append("restart preserves existing mode/config, ledger and synthetic internal Chromium cookie")
        report["image_id"] = docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
        report["checked_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["result"] = "passed"
        (ROOT / "artifacts/container-smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    except Exception:
        details = [docker("inspect", name, "--format", "{{json .State}}", check=False),
                   docker("exec", name, "supervisorctl", "-c", "/etc/supervisor/conf.d/relay.conf", "status", check=False)]
        logs = subprocess.run(["docker", "logs", "--tail", "200", name], text=True, capture_output=True, timeout=10)
        details.append(logs.stdout + logs.stderr)
        (ROOT / "artifacts/container-smoke-failure.log").write_text("\n".join(details).replace(password, "[redacted]"))
        print("Fixture diagnostics: artifacts/container-smoke-failure.log")
        raise
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", name, check=False)
        docker("network", "rm", name, check=False)


if __name__ == "__main__":
    main()
