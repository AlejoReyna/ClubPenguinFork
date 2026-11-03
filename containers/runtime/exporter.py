"""Expose aggregate game population; never use player names as metric labels."""
import os
import time

import redis
from prometheus_client import Gauge, start_http_server

players = Gauge('clubpenguin_players', 'Connected players in a world', ['world'])
redis_up = Gauge('clubpenguin_redis_up', 'Whether the population source is reachable')
connection = redis.Redis(host=os.environ.get('REDIS_HOST', 'redis'), socket_timeout=3)
start_http_server(9101)
while True:
    try:
        population = connection.hget('houdini.population', '3101')
        players.labels(world='Glaciar').set(int(population or 0))
        redis_up.set(1)
    except (redis.RedisError, ValueError):
        redis_up.set(0)
        # Do not display a stale player count as a current measurement.
        players.labels(world='Glaciar').set(float('nan'))
    time.sleep(15)

