"""Static checks on the production stack (launch plan v3, section 3):
docker-compose.prod.yml, the Caddyfile, the server .env template and the
nightly backup script."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "docker-compose.prod.yml"
CADDYFILE = ROOT / "Caddyfile"
ENV_TEMPLATE = ROOT / ".env.production.example"
BACKUP = ROOT / "scripts" / "backup.sh"


def _services() -> dict:
    return yaml.safe_load(PROD.read_text())["services"]


def _command(service: dict) -> str:
    cmd = service.get("command", "")
    return " ".join(cmd) if isinstance(cmd, list) else cmd


def test_only_caddy_faces_the_internet():
    """Caddy runs on the host's network so it sees visitors' real IPv4 and
    IPv6 addresses (Docker's IPv6 proxy hides them behind its gateway, which
    would put every IPv6 visitor in one rate-limit bucket). Everything else
    publishes on the host's loopback at most."""
    services = _services()
    assert services["caddy"].get("network_mode") == "host"
    assert not services["caddy"].get("ports")
    for name, service in services.items():
        if name == "caddy":
            continue
        ports = [str(p) for p in service.get("ports", [])]
        assert all(p.startswith("127.0.0.1:") for p in ports), (name, ports)


def test_every_image_is_pinned():
    for name, service in _services().items():
        image = service.get("image")
        if image is None:
            assert "build" in service, name
            continue
        assert ":latest" not in image and (":" in image or "@sha256:" in image), (name, image)


def test_no_source_mounts_and_no_reload():
    for name, service in _services().items():
        for volume in service.get("volumes", []):
            assert not str(volume).startswith((".:", "./app")), (name, volume)
        assert "--reload" not in _command(service), name


def test_exactly_one_worker_and_no_mailpit():
    services = _services()
    workers = [n for n, s in services.items() if "app.worker" in _command(s)]
    assert workers == ["worker"]
    assert "replicas" not in str(services["worker"].get("deploy", {}))
    assert "mailpit" not in services


def test_api_trusts_caddy_for_the_client_ip():
    """Rate limits key on the visitor's IP. Behind Caddy that's only right
    if uvicorn reads X-Forwarded-For; the api is published on the host's
    loopback only, so only Caddy (and the host itself) can reach it."""
    api = _services()["api"]
    command = _command(api)
    assert "--proxy-headers" in command
    assert "--forwarded-allow-ips" in command
    assert [str(p) for p in api.get("ports", [])] == ["127.0.0.1:8000:8000"]


def test_secrets_come_from_the_server_env_not_the_file():
    text = PROD.read_text()
    assert "digest_pass" not in text
    assert "${POSTGRES_PASSWORD:?" in text
    for name in ("api", "worker"):
        assert ".env" in str(_services()[name].get("env_file")), name


def test_temporal_keeps_its_state_on_a_volume():
    temporal = _services()["temporal"]
    match = re.search(r"--db-filename\s+(\S+)", _command(temporal))
    assert match
    assert any(match.group(1).startswith(str(v).split(":")[1]) for v in temporal["volumes"])


def test_caddy_serves_the_domain_and_redirects_www():
    text = CADDYFILE.read_text()
    assert re.search(r"^papersweekly\.com \{", text, re.M)
    assert "reverse_proxy 127.0.0.1:8000" in text
    assert re.search(r"^www\.papersweekly\.com \{\s*redir https://papersweekly\.com\{uri\}", text, re.M)


def test_env_template_lists_every_secret_without_values():
    keys = {}
    for line in ENV_TEMPLATE.read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            keys[key] = value
    for key in (
        "POSTGRES_PASSWORD", "SECRET_KEY", "ADMIN_TOKEN", "OPENAI_API_KEY",
        "SMTP_PASSWORD", "TURNSTILE_SECRET_KEY", "ALERT_EMAIL",
    ):
        assert keys.get(key, None) == "", key
    assert keys["PUBLIC_BASE_URL"] == "https://papersweekly.com"
    assert keys["SMTP_HOST"] == "smtp.gmail.com"


def test_backup_dumps_nightly_and_keeps_14_days():
    text = BACKUP.read_text()
    assert text.startswith("#!/")
    assert "pg_dump" in text and "-Fc" in text
    assert "-mtime +14" in text
    assert BACKUP.stat().st_mode & 0o111, "backup.sh must be executable"
