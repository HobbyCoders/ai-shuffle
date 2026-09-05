FROM postgres:16-alpine
RUN apk add --no-cache bash tar gzip
COPY docker/backup-entrypoint.sh /usr/local/bin/backup-entrypoint
RUN chmod +x /usr/local/bin/backup-entrypoint
ENTRYPOINT ["/usr/local/bin/backup-entrypoint"]
CMD []
