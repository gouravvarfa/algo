#!/bin/bash
# ONE-TIME, run on the server as the admin user (ubuntu):   curl -fsSL https://raw.githubusercontent.com/gouravvarfa/algo/main/deploy/setup-deploy-user.sh | sudo bash
# Makes a limited 'deploy' user that can run exactly one root-owned script (/usr/local/sbin/algo-deploy). GitHub Actions uses it to deploy.
# The key below is the PUBLIC half; the private half lives only in the GitHub secret DEPLOY_SSH_KEY.
set -euo pipefail
PUB='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHpxSgpkVhIozsYdPXc+jlnjujPq5sWV1IpVP+4ShU9G github-actions-deploy@algo'
RAW=https://raw.githubusercontent.com/gouravvarfa/algo/main/deploy/deploy.sh

id deploy >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
echo "restrict $PUB" > /home/deploy/.ssh/authorized_keys          # 'restrict': no port forwarding, no agent, no terminal
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys

curl -fsSL "$RAW" -o /tmp/algo-deploy.sh
install -m 755 -o root -g root /tmp/algo-deploy.sh /usr/local/sbin/algo-deploy   # a root-owned copy: the app user cannot edit what root runs
rm -f /tmp/algo-deploy.sh
echo 'deploy ALL=(root) NOPASSWD: /usr/local/sbin/algo-deploy' > /etc/sudoers.d/algo-deploy
chmod 440 /etc/sudoers.d/algo-deploy
visudo -c
echo "deploy user ready"
