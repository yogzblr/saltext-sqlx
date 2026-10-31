#!/usr/bin/env bash
# Install the extension from the read-only bind mount of the repository (/src), then run salt-minion in the
# foreground. Restarting the container reinstalls it, so a code change needs only `docker compose restart <minion>`.
# The tree is copied first: building in /src would write build/, *.egg-info and version.py into the repository as root.
set -euo pipefail
if [ -d /src/src/saltext ]; then
  rm -rf /tmp/src && mkdir -p /tmp/src
  tar -C /src --exclude=.git --exclude=.venv --exclude=.nox --exclude='*.egg-info' --exclude=build -cf - . | tar -C /tmp/src -xf -
  # setuptools_scm needs git metadata, which is not copied; the version is irrelevant here.
  SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+e2e salt-pip install --no-cache-dir --force-reinstall --no-deps /tmp/src >/var/log/sqlx-install.log 2>&1
  SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+e2e salt-pip install --no-cache-dir "/tmp/src[${SQLX_EXTRAS:-postgres,mysql,oracle,mssql}]" >>/var/log/sqlx-install.log 2>&1
fi
exec salt-minion -l "${SALT_LOG_LEVEL:-info}"
