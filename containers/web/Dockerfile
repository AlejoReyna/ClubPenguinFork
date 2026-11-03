# syntax=docker/dockerfile:1
FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=media media /srv/media
COPY --from=media play /srv/play
COPY --from=ruffle . /srv/ruffle
COPY default.conf /etc/nginx/conf.d/default.conf
COPY index.html register.html /srv/play/
EXPOSE 8080

