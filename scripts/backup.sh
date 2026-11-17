#!/bin/sh
# Nightly database dump on the production server (launch plan v3, section 3).
# Run by root's crontab (docs/deploy.md):
#   15 3 * * * /opt/papersweekly/scripts/backup.sh >> /var/log/papersweekly-backup.log 2>&1
# Keeps 14 days of custom-format dumps in BACKUP_DIR, on the server itself.
# Copy one to the Mac now and then for an off-server copy (docs/deploy.md).
set -eu

APP_DIR=${APP_DIR:-/opt/papersweekly}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/papersweekly}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$BACKUP_DIR/digest_db_$stamp.dump"

mkdir -p "$BACKUP_DIR"
cd "$APP_DIR"
# Write to a temp name first so a failed dump never looks like a good one.
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U digest_user -d digest_db -Fc > "$out.partial"
mv "$out.partial" "$out"
find "$BACKUP_DIR" -name 'digest_db_*.dump' -mtime +14 -delete
echo "$(date -u) backed up to $out ($(du -h "$out" | cut -f1))"
