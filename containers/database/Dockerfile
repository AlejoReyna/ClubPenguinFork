# syntax=docker/dockerfile:1
FROM postgres:16-bookworm
COPY --from=houdini houdini.sql /docker-entrypoint-initdb.d/001-houdini.sql
COPY 002-lab-starting-coins.sql /docker-entrypoint-initdb.d/002-lab-starting-coins.sql

