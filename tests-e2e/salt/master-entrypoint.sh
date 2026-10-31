#!/usr/bin/env bash
# salt-master in the foreground, salt-api (rest_cherrypy, plain HTTP on 8000, published on 127.0.0.1 only) beside it.
set -euo pipefail
salt-api -d -l "${SALT_LOG_LEVEL:-info}"
exec salt-master -l "${SALT_LOG_LEVEL:-info}"
