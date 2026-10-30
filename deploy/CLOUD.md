# Teatro on a cloud VM

Oracle Always Free is a possible zero-cost option, not an uptime guarantee. Create only resources labeled Always Free eligible and stay within the current allowance. Trial credits expire. Capacity and idle-resource reclamation can interrupt service. Do not create artificial load to evade reclamation. A budget alert is not a spending cap.

## Account and VM

1. Sign up at https://www.oracle.com/cloud/free/ and enable MFA. A payment card may be required for verification. Keep the free account unless you deliberately choose paid billing.
2. Create an Always Free eligible Ubuntu VM in your home region. Confirm the image, compute and disk are eligible before creating it. Start with at least 1 GB RAM. ARM is supported by the base image; the container build still needs verification on your VM.
3. Use an SSH key. Save the private key locally with mode 600; never upload it to GitHub. Allow inbound TCP 22 from your public IP only in the cloud network rules. Teatro needs outbound HTTPS and DNS; it needs no public HTTP ports.
4. SSH into the VM as its Ubuntu user. Keep the OS patched. Install Docker Engine and the Compose plugin using https://docs.docker.com/engine/install/ubuntu/ . Use sudo for Docker commands rather than granting untrusted users Docker access.

## Transfer and start

Upload the project files to ~/Teatro using Git or scp. Never include .env, database files, or your SSH key in Git. If the repository is private, use a read-only deploy key for cloning.

On the VM, in ~/Teatro:

```sh
cp -n .env.example .env
chmod 600 .env
nano .env
```

Enter the Discord token and API keys privately. Set both GUILD_ID and ALLOWED_GUILD_ID to your server ID. Compose forces DB_PATH=/data/filmclub.db; that directory is a persistent Docker volume.

```sh
sudo docker compose build
```

Before starting in the cloud, stop Teatro on your laptop with Control+C. Two instances using the same bot token can duplicate posts even if they have different databases.

To preserve your current setup, while the laptop bot is stopped:

```sh
.venv/bin/python deploy/backup.py filmclub.db /tmp/teatro-transfer.db
```

If your local DB_PATH differs, use that path instead. Copy this snapshot to ~/Teatro/teatro-transfer.db on the VM with scp. Then initialize the cloud volume:

```sh
sudo docker compose run --rm --user 0 --volume "$PWD/teatro-transfer.db:/tmp/import.db:ro" teatro sh -c 'test ! -e /data/filmclub.db && cp /tmp/import.db /data/filmclub.db && chown 10001:10001 /data/filmclub.db && chmod 600 /data/filmclub.db'
sudo docker compose up -d
sudo docker compose logs --tail=50 teatro
```

The import refuses to overwrite an existing database. If starting fresh, skip the import and run up -d. Confirm login and run /health, /search and /movienight status in Discord. You can disconnect SSH; Docker keeps the bot running. Enable Docker at boot with `sudo systemctl enable --now docker`.

## Backups and updates

Create a consistent snapshot inside the running container, then copy it to the host:

```sh
mkdir -p backups
chmod 700 backups
sudo docker compose exec -T teatro python -c 'import sqlite3; s=sqlite3.connect("/data/filmclub.db"); d=sqlite3.connect("/data/snapshot.db"); s.backup(d); d.close(); s.close()'
sudo docker compose cp teatro:/data/snapshot.db backups/snapshot.db
sudo chmod 600 backups/snapshot.db
```

Copy backups to a private location outside this VM; a backup on a reclaimed VM is lost too. Schedule the same backup commands daily through the VM's cron, using absolute paths and `cd` to this project directory. Keep dated copies and prune to a retention period you choose (for example seven days). Test restoration before relying on backups. Do not publish the database: it includes member activity.

To restore, stop the service, preserve the current database, and restore the verified snapshot while no bot process is running. SQLite WAL/SHM files belong to the old database and must not be reused with a restored snapshot.

To update, back up first, transfer updated source files, and run:

```sh
sudo docker compose up -d --build
sudo docker compose logs --tail=50 teatro
```

Never run `docker compose down -v` unless you intend to delete all persistent bot data.

## Security

- Grant Discord's specific permissions, not Administrator. Keep the Teatro role just above FilmClub.
- Keep your application private in the Developer Portal if appropriate for your server.
- Rotate a token immediately if exposed; update .env and recreate the container with `sudo docker compose up -d --force-recreate`.
- Secrets are excluded from the image build context and Git. Host administrators can still inspect container environment variables; protect the cloud account and SSH keys.
- The container runs as UID 10001, has a read-only root filesystem, drops capabilities, cannot gain new privileges, and exposes no ports.
- The cloud entry point requires a guild allowlist; slash commands and message components enforce it. This is a server restriction, not a substitute for host/admin role checks.
- Local file locking prevents two processes opening the same database; it cannot stop a second bot on another machine.
- Check /health regularly, cloud notifications, and rotated logs. Docker restarts crashed processes, but a hung worker or provider outage can still require attention.
- Review dependency and base-image security updates regularly. Pinned core versions improve reproducibility but are not a security audit.

This configuration has offline Python checks; build and live Discord validation must be completed on your selected host.
