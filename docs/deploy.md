# Deploying papersweekly.com

One Hetzner VM runs `docker-compose.prod.yml`: Caddy (HTTPS), the API, one
worker, Temporal and Postgres. These are the commands for launch plan v3,
section 3. On the server Compose is the `docker compose` plugin; on the Mac
it's the standalone `docker-compose` binary.

## 1. One-time setup in the consoles

**Hetzner Cloud**
1. Create a project and add your SSH public key (`~/.ssh/id_ed25519.pub`).
2. Create a server:
   - **Type and image:** CX23, Ubuntu 24.04.
   - **Location:** Nuremberg or Helsinki.
   - **Network:** public IPv4 and IPv6.
   - **Backups:** off (owner's call, 2026-11-17). The nightly dumps stay on the server, and you copy one to the Mac now and then.
3. Create a firewall for the server that allows inbound TCP 22, 80 and 443, and UDP 443. Allow nothing else.

**Cloudflare DNS** (papersweekly.com). Every record is **DNS only** (grey cloud), so Caddy can get its own certificate:

| Type | Name | Value |
|---|---|---|
| A | `@` | the server's IPv4 |
| AAAA | `@` | the server's IPv6 |
| CNAME | `www` | `papersweekly.com` |

**Cloudflare Turnstile:** in the widget's settings, add `papersweekly.com` to its hostnames. `www` redirects, so it doesn't need adding.

## 2. Prepare the server

```bash
ssh root@SERVER_IP
apt update && apt -y upgrade
apt -y install docker.io docker-compose-v2 git
git clone https://github.com/kian-hekmat/research-digest-agent.git /opt/papersweekly
cd /opt/papersweekly
cp .env.production.example .env && chmod 600 .env
nano .env    # fill in every value (generate the secrets as the file says)
```

## 3. Move the database from the Mac

On the Mac:
1. Pause `daily-topic-digests`, `send-prep` and `weekly-digest-emails`:
   `docker-compose exec -T temporal temporal schedule toggle --schedule-id <id> --pause --reason move`.
2. Stop the worker: `docker-compose stop worker`.
3. Dump the database and copy it to the server:
   ```bash
   docker-compose exec -T db pg_dump -U digest_user -d digest_db -Fc > ~/research-digest-backups/move.dump
   scp ~/research-digest-backups/move.dump root@SERVER_IP:/root/
   ```

On the server, in `/opt/papersweekly`:

```bash
docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml exec -T db pg_restore -U digest_user -d digest_db --no-owner < /root/move.dump
docker compose -f docker-compose.prod.yml run --rm worker alembic upgrade head
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs --tail=20 worker   # "Workers polling ..."
```

The worker recreates the three schedules on a fresh Temporal, unpaused. Leave
the Mac's worker stopped for good, so two workers never send.

## 4. Nightly backups

```bash
crontab -e   # add:
15 3 * * * /opt/papersweekly/scripts/backup.sh >> /var/log/papersweekly-backup.log 2>&1
```

Check the setup by running `scripts/backup.sh` once by hand. It keeps 14 days
in `/var/backups/papersweekly`, on the server only. For an off-server copy,
pull the newest dump to the Mac now and then:

```bash
scp "root@SERVER_IP:$(ssh root@SERVER_IP 'ls -t /var/backups/papersweekly/*.dump | head -1')" ~/research-digest-backups/
```

## Everyday operations

- **Deploy:** `git pull && docker compose -f docker-compose.prod.yml up -d --build`.
  - If the pull added a migration, run `docker compose -f docker-compose.prod.yml run --rm worker alembic upgrade head` first.
  - If it changes a workflow, first check nothing is running, then stop the worker.
- **Temporal UI:** `ssh -L 8233:127.0.0.1:8233 root@SERVER_IP`, then open http://localhost:8233.
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f api worker caddy`.
- **Database shell:** `docker compose -f docker-compose.prod.yml exec db psql -U digest_user -d digest_db`.
- **Restore a dump:** `pg_restore --clean --no-owner` into the `db` service, as in step 3.
