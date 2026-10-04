"""Expose Elasticsearch JSON bodies before validating their structure."""
from elastic_transport import ObjectApiResponse


def response_body(response):
    return response.body if isinstance(response, ObjectApiResponse) else response
