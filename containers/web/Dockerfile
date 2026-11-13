# syntax=docker/dockerfile:1
FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=media media /srv/media
COPY --from=media play /srv/play
# The page loads the dump's own AS3 wrapper (club_penguin.swf), which reads this file for its
# paths. The dump's copy points at media1.clubpenguin.com; ours points at this site through the
# page's http://localhost rewrite rule, so the same image works on any hostname.
COPY environment_data.xml /srv/media/play/web_service/environment_data.xml
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

