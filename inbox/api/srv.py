from flask import Flask, request, jsonify, make_response, g
from flask.ext.restful import reqparse
from werkzeug.exceptions import default_exceptions, HTTPException
from sqlalchemy.orm.exc import NoResultFound

from inbox.api.kellogs import APIEncoder
from nylas.logging import get_logger
from inbox.models import Namespace, Account
from inbox.models.session import global_session_scope
from inbox.api.validation import (bounded_str, ValidatableArgument,
                                  strict_parse_args, limit)
from inbox.api.validation import valid_public_id

from ns_api import app as ns_api
from ns_api import DEFAULT_LIMIT

from inbox.webhooks.gpush_notifications import app as webhooks_api

app = Flask(__name__)
# Handle both /endpoint and /endpoint/ without redirecting.
# Note that we need to set this *before* registering the blueprint.
app.url_map.strict_slashes = False


def default_json_error(ex):
    """ Exception -> flask JSON responder """
    logger = get_logger()
    logger.error('Uncaught error thrown by Flask/Werkzeug', exc_info=ex)
    response = jsonify(message=str(ex), type='api_error')
    response.status_code = (ex.code
                            if isinstance(ex, HTTPException)
                            else 500)
    return response

# Patch all error handlers in werkzeug
for code in default_exceptions.iterkeys():
    app.error_handler_spec[None][code] = default_json_error


@app.before_request
def auth():
    """ Check for account ID on all non-root URLS """
    if request.path in ('/accounts', '/accounts/', '/') \
            or request.path.startswith('/w/'):
        return

    if not request.authorization or not request.authorization.username:

        AUTH_ERROR_MSG = ("Could not verify access credential.", 401,
                          {'WWW-Authenticate': 'Basic realm="API '
                              'Access Token Required"'})

        auth_header = request.headers.get('Authorization', None)

        if not auth_header:
            return make_response(AUTH_ERROR_MSG)

        parts = auth_header.split()

        if (len(parts) != 2 or parts[0].lower() != 'bearer' or not parts[1]):
            return make_response(AUTH_ERROR_MSG)
        namespace_public_id = parts[1]

    else:
        namespace_public_id = request.authorization.username

    with global_session_scope() as db_session:
        try:
            valid_public_id(namespace_public_id)
            namespace = db_session.query(Namespace) \
                .filter(Namespace.public_id == namespace_public_id).one()
            g.namespace_id = namespace.id
            g.account_id = namespace.account.id
        except NoResultFound:
            return make_response((
                "Could not verify access credential.", 401,
                {'WWW-Authenticate': 'Basic realm="API '
                 'Access Token Required"'}))


@app.after_request
def finish(response):
    origin = request.headers.get('origin')
    if origin:  # means it's just a regular request
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = \
            'Authorization,Content-Type'
        response.headers['Access-Control-Allow-Methods'] = \
            'GET,PUT,POST,DELETE,OPTIONS,PATCH'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response


@app.route('/accounts/')
def ns_all():
    """ Return all namespaces """
    # We do this outside the blueprint to support the case of an empty
    # public_id.  However, this means the before_request isn't run, so we need
    # to make our own session
    with global_session_scope() as db_session:
        parser = reqparse.RequestParser(argument_class=ValidatableArgument)
        parser.add_argument('limit', default=DEFAULT_LIMIT, type=limit,
                            location='args')
        parser.add_argument('offset', default=0, type=int, location='args')
        parser.add_argument('email_address', type=bounded_str, location='args')
        args = strict_parse_args(parser, request.args)

        query = db_session.query(Namespace)
        if args['email_address']:
            query = query.join(Account)
            query = query.filter_by(email_address=args['email_address'])

        query = query.limit(args['limit'])
        if args['offset']:
            query = query.offset(args['offset'])

        namespaces = query.all()
        encoder = APIEncoder()
        return encoder.jsonify(namespaces)


@app.route('/logout')
def logout():
    """ Utility function used to force browsers to reset cached HTTP Basic Auth
        credentials """
    return make_response((
        "<meta http-equiv='refresh' content='0; url=/''>.",
        401,
        {'WWW-Authenticate': 'Basic realm="API Access Token Required"'}))


app.register_blueprint(ns_api)
app.register_blueprint(webhooks_api)  # /w/...
