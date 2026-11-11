"""Content negotiation shared by authentication and browser error recovery."""
from flask import request


def wants_json():
    return request.is_json or request.accept_mimetypes.best == 'application/json'
