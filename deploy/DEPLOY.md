# CI / CD: how it works and the one-time setup

**CI** (`.github/workflows/ci.yml`) runs on every push and pull request: backend tests, frontend type-check + tests + build, and a check that no secret is tracked. Nothing to set up.

**CD** (`.github/workflows/deploy.yml`) deploys `main` to the server after CI passes. It stays OFF until you do the steps below.

## One-time setup on the server (as the admin `ubuntu` user)
1. Make a limited user that can only run the deploy script:
   ```
   sudo adduser --disabled-password --gecos "" deploy
   sudo install -m 755 -o root -g root /opt/algo-trading/deploy/deploy.sh /usr/local/sbin/algo-deploy
   echo 'deploy ALL=(root) NOPASSWD: /usr/local/sbin/algo-deploy' | sudo tee /etc/sudoers.d/algo-deploy
   sudo chmod 440 /etc/sudoers.d/algo-deploy && sudo visudo -c
   ```
   (Run the `install` line again whenever `deploy/deploy.sh` changes: the server uses a root-owned copy on purpose.)
2. On your laptop make a key used ONLY for deploys: `ssh-keygen -t ed25519 -f algo-deploy -N ""`. Put `algo-deploy.pub` into `/home/deploy/.ssh/authorized_keys` (chmod 700 .ssh, 600 the file, owner deploy).

## One-time setup on GitHub (repo > Settings > Secrets and variables > Actions)
- Secrets: `DEPLOY_HOST` (52.66.143.58), `DEPLOY_USER` (deploy), `DEPLOY_SSH_KEY` (the content of `algo-deploy`, the PRIVATE file), `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan -t ed25519 52.66.143.58`).
- Variable: `DEPLOY_ENABLED` = `true`.
- Optional: Settings > Environments > `production` > "Required reviewers" so every deploy waits for your click.

## Rollback
If the health check after a deploy fails, the script goes back to the previous commit by itself and the workflow turns red. To go back by hand: `sudo /usr/local/sbin/algo-deploy <old-commit>`.

## What a deploy never touches
`data/`, `/etc/algo-trading.env`, `angelone_credentials.json`, the users and their history.
