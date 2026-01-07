from redis import Redis
from rq import Queue


def get_redis_connection(app=None):
    if app is None:
        from flask import current_app
        app = current_app
    redis_url = app.config.get('REDIS_URL', 'redis://localhost:6379/0')
    return Redis.from_url(redis_url)


def get_queue(app=None):
    if app is None:
        from flask import current_app
        app = current_app
    queue_name = app.config.get('RQ_QUEUE_NAME', 'sms')
    return Queue(queue_name, connection=get_redis_connection(app))
