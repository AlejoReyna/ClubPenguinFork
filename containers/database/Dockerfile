# syntax=docker/dockerfile:1
FROM postgres:16-bookworm
COPY --from=houdini houdini.sql /docker-entrypoint-initdb.d/001-houdini.sql

