#!/bin/sh
# Renders the Asterisk config from env vars (so the SIP password never lives in git) and starts Asterisk.
set -e
: "${SIP_USER:?SIP_USER is required}" "${SIP_PASSWORD:?SIP_PASSWORD is required}"
export SIP_HOST="${SIP_HOST:-103.251.120.244}"
export SIP_PORT="${SIP_PORT:-5060}"
export SIP_LOCAL_PORT="${SIP_LOCAL_PORT:-5062}"
export SIP_RTP_START="${SIP_RTP_START:-10000}"
export SIP_RTP_END="${SIP_RTP_END:-10400}"
export SIP_MAX_CALL_SECONDS="${SIP_MAX_CALL_SECONDS:-300}"
export AUDIOSOCKET_ADDR="${AUDIOSOCKET_ADDR:-127.0.0.1:8092}"
VARS='${SIP_USER} ${SIP_PASSWORD} ${SIP_HOST} ${SIP_PORT} ${SIP_LOCAL_PORT} ${SIP_RTP_START} ${SIP_RTP_END} ${SIP_MAX_CALL_SECONDS} ${AUDIOSOCKET_ADDR}'
for f in pjsip.conf extensions.conf rtp.conf; do
  envsubst "$VARS" < "/etc/asterisk.tmpl/$f" > "/etc/asterisk/$f"
done
for f in modules.conf logger.conf manager.conf http.conf; do
  cp "/etc/asterisk.tmpl/$f" "/etc/asterisk/$f"
done
chown root:asterisk /etc/asterisk/*.conf
chmod 640 /etc/asterisk/pjsip.conf
mkdir -p /var/spool/asterisk/outgoing /var/spool/asterisk/outgoing_done /var/spool/asterisk/tmp
chown -R asterisk:asterisk /var/spool/asterisk
chmod 777 /var/spool/asterisk/tmp
exec asterisk -f -U asterisk -G asterisk -v
