# syntax=docker/dockerfile:1
FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=media media /srv/media
COPY --from=media play /srv/play
# The 2017 dump has no boots.swf. This loader only points Ruffle at /play/v2/client/.
COPY boots.swf /srv/media/boots.swf
# load.swf from the legacy media, pinned: with the 2017 dump's load.swf this boots.swf never
# loads shell.swf (blank stage). This is the copy the site has served since Nov 3.
COPY load.swf /srv/media/play/v2/client/load.swf
COPY custom-clothing/ /srv/media/play/v2/content/global/clothing/
COPY --from=ruffle . /srv/ruffle
# Cache-buster hashes only. The 2017 engine/login/create SWFs stay the media originals:
# the puffle patches were written for the older client and would replace this one.
COPY --from=puffle-client companion.zip /srv/play/puffle/companion.zip
COPY --from=puffle-client build.json /srv/play/puffle/build.json
COPY default.conf /etc/nginx/conf.d/default.conf
COPY index.html register.html manifest.webmanifest sw.js dialog-bots-mentions.json /srv/play/
COPY puffle.html /srv/play/puffle/index.html
COPY icons/ /srv/play/icons/
# The media dump leaves global/clothing as root-only; unprivileged nginx must traverse it.
USER 0
RUN chmod -R a+rX /srv/media/play/v2/content/global/clothing
USER 101
EXPOSE 8080

