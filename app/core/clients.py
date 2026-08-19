import redis
from elasticsearch import Elasticsearch

from app.core.config import settings


elasticsearch_client = Elasticsearch(
    settings.elasticsearch_url,
    request_timeout=2,
)


redis_client = redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)