# syntax=docker/dockerfile:1
FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=media media /srv/media
COPY --from=media play /srv/play
COPY custom-clothing/ /srv/media/play/v2/content/global/clothing/
COPY --from=ruffle . /srv/ruffle
COPY --from=puffle-client engine.swf /srv/media/play/v2/client/engine.swf
COPY --from=puffle-client companion.zip /srv/play/puffle/companion.zip
COPY --from=puffle-client build.json /srv/play/puffle/build.json
COPY default.conf /etc/nginx/conf.d/default.conf
COPY index.html register.html manifest.webmanifest sw.js /srv/play/
COPY puffle.html /srv/play/puffle/index.html
COPY icons/ /srv/play/icons/
EXPOSE 8080

