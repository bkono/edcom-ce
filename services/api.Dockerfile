FROM python-base:latest

EXPOSE 8000
EXPOSE 5678

VOLUME /buckets

RUN ln -s `find / -name gunicorn -type f` /usr/bin/gunicorn

COPY ./config/logrotate.conf /etc/logrotate.conf
COPY ./config/gunicorn-logging.conf /etc/gunicorn-logging.conf
COPY ./config/crontab /crontab
RUN /usr/bin/crontab /crontab

COPY ./api/ /api/
COPY ./scripts /scripts/

ENV redis_host=cache
ENV redis_pass=
ENV redis_port=6379
ENV postgres_conn=postgres://edcom:edcom@database:6432/edcom
ENV queue_proto=redis
ENV queue_host=cache
ENV queue_user=
ENV queue_pass=
ENV queue_db=0
ENV keygen_account_id=5b5914e5-3f64-439e-af80-b4404f2eb853
ENV keygen_product_id=d1fe386f-6af6-4d92-ba63-a194072f27b6
ENV webroot=http://localhost:3000
ENV s3_databucket=/buckets/data
ENV s3_transferbucket=/buckets/transfer
ENV s3_imagebucket=/buckets/images
ENV s3_blockbucket=/buckets/blocks
ENV attachment_enabled=false
ENV attachment_storage_backend=s3
ENV attachment_s3_bucket=
ENV attachment_s3_prefix=attachments/txn/
ENV attachment_s3_region=
ENV attachment_s3_access_key=
ENV attachment_s3_secret_key=
ENV attachment_s3_endpoint_url=
ENV attachment_local_path=/buckets/attachments
ENV attachment_manage_lifecycle=true
ENV attachment_ttl_hours=24
ENV attachment_lifecycle_expiration_days=2
ENV attachment_lifecycle_abort_multipart_days=1
ENV attachment_max_count=5
ENV attachment_max_file_bytes=10485760
ENV attachment_max_total_bytes=20971520
ENV attachment_max_mime_bytes=31457280
ENV attachment_json_body_max_bytes=33554432
ENV mg_validate_key=
ENV pixabay_key=
ENV zendesk_host=
ENV zendesk_user=
ENV zendesk_key=
ENV support_email=

CMD sh -c "/scripts/run_db_migrations.py && /usr/bin/env gunicorn --reload --bind 0.0.0.0:8000 -t 300000 -w `/scripts/num_tasks.py gunicorn` --log-config /etc/gunicorn-logging.conf --access-logformat '\"%({X-Forwarded-For}i)s\" \"%(a)s\" %(t)s \"%(r)s\" %(s)s %(b)s %(L)s' api.app"
