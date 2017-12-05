from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import json
import logging

from builtins import str
from klein import Klein
from typing import Union, Text, Optional

from rasa_core.agent import Agent
from rasa_core.events import Event
from rasa_core.interpreter import NaturalLanguageInterpreter
from rasa_core.version import __version__
from rasa_nlu.server import check_cors

logger = logging.getLogger(__name__)


def create_argument_parser():
    parser = argparse.ArgumentParser(
            description='starts server to serve an agent')
    parser.add_argument(
            '-d', '--core',
            required=True,
            type=str,
            help="core model to run with the server")
    parser.add_argument(
            '-u', '--nlu',
            type=str,
            help="nlu model to run with the server")
    parser.add_argument(
            '-p', '--port',
            type=int,
            default=5005,
            help="port to run the server at")
    parser.add_argument(
            '--cors',
            nargs='*',
            type=str,
            help="enable CORS for the passed origin. "
                 "Use * to whitelist all origins")
    parser.add_argument(
            '-o', '--log_file',
            type=str,
            default="rasa_core.log",
            help="store log file in specified file")

    # arguments for logging configuration
    parser.add_argument(
            '--debug',
            help="Print lots of debugging statements. "
                 "Sets logging level to DEBUG",
            action="store_const",
            dest="loglevel",
            const=logging.DEBUG,
            default=logging.WARNING,
    )
    parser.add_argument(
            '-v', '--verbose',
            help="Be verbose. Sets logging level to INFO",
            action="store_const",
            dest="loglevel",
            const=logging.INFO,
    )
    return parser


def convert_obj_2_tracker_events(serialized_events, domain):
    # Example format: {"event": "set_slot", "value": 5, "name": "my_slot"}

    deserialized = []
    for e in serialized_events:
        etype = e.get("event")
        if etype is not None:
            del e["event"]
            deserialized.append(Event.from_parameters(etype, e, domain))
    return deserialized


class RasaCoreServer(object):
    """Class representing a Rasa Core HTTP server."""

    app = Klein()

    def __init__(self, model_directory,
                 interpreter=None,
                 loglevel="INFO",
                 log_file="rasa_core.log",
                 cors_origins=None,
                 action_factory=None):
        logging.basicConfig(filename=log_file, level=loglevel)
        logging.captureWarnings(True)

        self.config = {"cors_origins": cors_origins if cors_origins else []}
        self.agent = self._create_agent(model_directory, interpreter,
                                        action_factory)

    @staticmethod
    def _create_agent(
            model_directory,  # type: Text
            interpreter,  # type: Union[Text, NaturalLanguageInterpreter]
            action_factory=None #type: Optional[Text]
    ):
        # type: (...) -> Agent
        return Agent.load(model_directory, interpreter,
                          action_factory=action_factory)

    @app.route("/", methods=['GET', 'OPTIONS'])
    @check_cors
    def hello(self, request):
        """Check if the server is running and responds with the version."""
        return "hello from Rasa Core: " + __version__

    @app.route("/conversations/<cid>/continue", methods=['POST', 'OPTIONS'])
    @check_cors
    def continue_predicting(self, request, cid):
        request.setHeader('Content-Type', 'application/json')
        request_params = json.loads(
                request.content.read().decode('utf-8', 'strict'))
        encoded_events = request_params.get("events", [])
        executed_action = request_params.get("executed_action", None)
        events = convert_obj_2_tracker_events(encoded_events,
                                              self.agent.domain)
        response = self.agent.continue_message_handling(cid,
                                                        executed_action,
                                                        events)
        return json.dumps(response)

    @app.route("/conversations/<cid>/tracker/events", methods=['POST',
                                                               'OPTIONS'])
    @check_cors
    def append_events(self, request, cid):
        """Append a list of events to the state of a conversation"""
        request.setHeader('Content-Type', 'application/json')
        request_params = json.loads(
                request.content.read().decode('utf-8', 'strict'))
        events = convert_obj_2_tracker_events(request_params,
                                              self.agent.domain)
        tracker = self.agent.tracker_store.get_or_create_tracker(cid)
        for e in events:
            tracker.update(e)
        self.agent.tracker_store.save(tracker)
        return json.dumps(tracker.current_state())

    @app.route("/conversations/<cid>/tracker", methods=['GET', 'OPTIONS'])
    @check_cors
    def retrieve_tracker(self, request, cid):
        """Get a dump of a conversations tracker including its events."""

        request.setHeader('Content-Type', 'application/json')
        tracker = self.agent.tracker_store.get_or_create_tracker(cid)
        return json.dumps(tracker.current_state(should_include_events=True))

    @app.route("/conversations/<cid>/tracker", methods=['PUT', 'OPTIONS'])
    @check_cors
    def update_tracker(self, request, cid):
        """Use a list of events to set a conversations tracker to a state."""

        request.setHeader('Content-Type', 'application/json')
        request_params = json.loads(
                request.content.read().decode('utf-8', 'strict'))
        events = convert_obj_2_tracker_events(request_params,
                                              self.agent.domain)
        tracker = self.agent.tracker_store.create_tracker(cid)
        for e in events:
            tracker.update(e)

        # will override an existing tracker with the same id!
        self.agent.tracker_store.save(tracker)
        return json.dumps(tracker.current_state(should_include_events=True))

    @app.route("/conversations/<cid>/parse", methods=['GET', 'POST', 'OPTIONS'])
    @check_cors
    def parse(self, request, cid):
        request.setHeader('Content-Type', 'application/json')
        if request.method.decode('utf-8', 'strict') == 'GET':
            request_params = {
                key.decode('utf-8', 'strict'): value[0].decode('utf-8',
                                                               'strict')
                for key, value in request.args.items()}
        else:
            request_params = json.loads(
                    request.content.read().decode('utf-8', 'strict'))

        if 'query' in request_params:
            message = request_params.pop('query')
        elif 'q' in request_params:
            message = request_params.pop('q')
        else:
            request.setResponseCode(404)
            return json.dumps({"error": "Invalid parse parameter specified"})

        try:
            response = self.agent.start_message_handling(message, cid)
            request.setResponseCode(200)
            return json.dumps(response)
        except Exception as e:
            request.setResponseCode(500)
            logger.error("Caught an exception during "
                         "parse: {}".format(e), exc_info=1)
            return json.dumps({"error": "{}".format(e)})

    @app.route("/version", methods=['GET', 'OPTIONS'])
    @check_cors
    def version(self, request):
        """Respond with the version number of the installed Rasa Core."""

        request.setHeader('Content-Type', 'application/json')
        return json.dumps({'version': __version__})


if __name__ == '__main__':
    # Running as standalone python application
    arg_parser = create_argument_parser()
    cmdline_args = arg_parser.parse_args()

    logging.basicConfig(level=cmdline_args.loglevel)

    rasa = RasaCoreServer(cmdline_args.core,
                          cmdline_args.nlu,
                          cmdline_args.loglevel,
                          cmdline_args.log_file,
                          cmdline_args.cors)

    logger.info("Started http server on port %s" % cmdline_args.port)
    rasa.app.run("0.0.0.0", cmdline_args.port)
