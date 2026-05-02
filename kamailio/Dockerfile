FROM ghcr.io/kamailio/kamailio-ci:6.0.2-alpine

RUN rm -f /etc/kamailio/kamctlrc

COPY kamailio.cfg /etc/kamailio/kamailio.cfg
COPY config /etc/kamailio/config

COPY kamailio-entrypoint.sh /usr/local/bin/kamailio-entrypoint.sh
RUN chmod +x /usr/local/bin/kamailio-entrypoint.sh

# Install PostgreSQL client for psql
RUN apk add --no-cache postgresql-client sngrep


ENTRYPOINT ["/usr/local/bin/kamailio-entrypoint.sh"]
