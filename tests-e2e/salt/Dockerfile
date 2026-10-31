# Salt master / minion for the end-to-end suite (tests-e2e/README.md). One image, the role is chosen by the command.
# There is no official saltproject/salt image on Docker Hub (checked 2026-10-31: the repository does not exist), so the
# image is built from the Salt Project onedir tarball, the same artefact the salt-master / salt-minion packages ship.
FROM debian:bookworm-slim

ARG SALT_VERSION=3008.5
ARG SALT_ONEDIR_URL=https://packages.broadcom.com/artifactory/saltproject-generic/onedir/${SALT_VERSION}/salt-${SALT_VERSION}-onedir-linux-x86_64.tar.xz

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils procps iproute2 \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/saltstack \
 && curl -fsSL "$SALT_ONEDIR_URL" | tar -xJ -C /opt/saltstack \
 && for b in salt salt-api salt-call salt-key salt-master salt-minion salt-pip salt-run; do \
      ln -s /opt/saltstack/salt/$b /usr/local/bin/$b; done \
 && salt-call --version

# The drivers of every extra, installed once so that restarting the minion (which reinstalls the extension from the
# bind-mounted repository, see minion-entrypoint.sh) does not download them again. The extension's own install
# resolves the extras again and would add anything missing here.
RUN salt-pip install --no-cache-dir 'SQLAlchemy>=2.0,<2.1' 'psycopg[binary]' PyMySQL oracledb pymssql cryptography

COPY master-entrypoint.sh minion-entrypoint.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/master-entrypoint.sh /usr/local/bin/minion-entrypoint.sh
