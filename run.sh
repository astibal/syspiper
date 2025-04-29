#!/bin/sh
WORKDIR="${HOME}"
IP_PORT="0.0.0.0:8181"
"${WORKDIR}"/.venv/bin/gunicorn -w 4 -b "${IP_PORT}" wsgi:app