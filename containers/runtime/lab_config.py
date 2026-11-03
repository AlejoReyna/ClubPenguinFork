import os
from dash.settings import *  # Preserve upstream defaults; override only lab configuration.

ADDRESS = '0.0.0.0'
PORT = 3000
POSTGRES_HOST = os.environ.get('POSTGRES_HOST', 'postgres')
POSTGRES_NAME = os.environ.get('POSTGRES_DB', 'houdini')
POSTGRES_USER = os.environ.get('POSTGRES_USER', 'houdini')
POSTGRES_PASSWORD = os.environ['POSTGRES_PASSWORD']
REDIS_ADDRESS = os.environ.get('REDIS_HOST', 'redis')
REDIS_PORT = 6379
APPROVE_USERNAME = True
ACTIVATE_PLAYER = True
EMAIL_METHOD = ''
GSECRET_KEY = ''
EMAIL_WHITELIST = []
LEGACY_PLAY_LINK = os.environ.get('PLAY_URL', 'http://192.168.0.10:30080')
LEGACY_ACTIVATE_REDIRECT = LEGACY_PLAY_LINK

