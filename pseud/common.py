import functools
import inspect
import logging
import pprint
import textwrap
import traceback
import uuid

import dateutil.parser
import dateutil.tz
from future import standard_library
from future.builtins import str
import zmq
import zope.component
import zope.interface

with standard_library.hooks():
    import builtins

from . import interfaces  # NOQA
from .interfaces import (AUTHENTICATED,
                         EMPTY_DELIMITER,
                         ERROR,
                         HEARTBEAT,
                         HELLO,
                         IAuthenticationBackend,
                         IHeartbeatBackend,
                         OK,
                         ServiceNotFoundError,
                         UNAUTHORIZED,
                         VERSION,
                         WORK,
                         )  # NOQA
from .utils import (get_rpc_callable,
                    register_rpc,
                    create_local_registry,
                    )  # NOQA
from .packer import Packer  # NOQA


logger = logging.getLogger(__name__)

_marker = object()


internal_exceptions = tuple(name for name in dir(interfaces)
                            if inspect.isclass(getattr(interfaces, name))
                            and issubclass(getattr(interfaces, name),
                                           Exception))


class DummyFuture(object):
    """
    When future is gone replace it with this one to display
    incoming messages associating to ghost future.
    """
    def set_exception(self, exception):
        try:
            raise exception
        except:
            logger.exception('Captured exception from main loop')
            raise


def format_remote_traceback(traceback):
    pivot = '\n{}'.format(3 * 4 * ' ')  # like three tabs
    return textwrap.dedent("""
        -- Beginning of remote traceback --
            {}
        -- End of remote traceback --
        """.format(pivot.join(str(traceback).splitlines())))


UTC = dateutil.tz.tzutc()


class AttributeWrapper(object):
    def __init__(self, rpc, name=None, user_id=None):
        self.rpc = rpc
        self._part_names = name.split('.') if name is not None else []
        self.user_id = user_id

    def __getattr__(self, name, default=_marker):
        try:
            if default is _marker:
                return super(AttributeWrapper, self).__getattr__(name)
            return super(AttributeWrapper, self).__getattr__(name,
                                                             default=default)
        except AttributeError:
            self.name = name
            return self

    def name_getter(self):
        return '.'.join(self._part_names)

    def name_setter(self, value):
        self._part_names.append(value)

    name = property(name_getter, name_setter)

    def __call__(self, *args, **kw):
        user_id = self.user_id or self.rpc.peer_routing_id
        return self.rpc.send_work(user_id, self.name, *args, **kw)


class BaseRPC(object):
    def __init__(self, user_id=None, routing_id=None, peer_routing_id=None,
                 context=None, io_loop=None,
                 security_plugin='noop_auth_backend',
                 public_key=None, secret_key=None,
                 peer_public_key=None, timeout=5,
                 password=None, heartbeat_plugin='noop_heartbeat_backend',
                 proxy_to=None, registry=None, translation_table=None):
        self.user_id = user_id
        self.routing_id = routing_id
        self.context = context or self._make_context()
        self.peer_routing_id = peer_routing_id
        self.security_plugin = security_plugin
        self.future_pool = {}
        self.initialized = False
        self.auth_backend = zope.component.getAdapter(self,
                                                      IAuthenticationBackend,
                                                      name=self.security_plugin
                                                      )
        self.public_key = public_key
        self.secret_key = secret_key
        self.peer_public_key = peer_public_key
        self.password = password
        self.timeout = timeout
        self.heartbeat_backend = zope.component.getAdapter(
            self,
            IHeartbeatBackend,
            name=heartbeat_plugin)
        self.proxy_to = proxy_to
        self._backend_init(io_loop=io_loop)
        self.reader = None
        self.registry = (registry if registry is not None
                         else create_local_registry(user_id or ''))
        self.socket = None
        self.packer = Packer(translation_table)

    def __getattr__(self, name, default=_marker):
        try:
            if default is _marker:
                return super(BaseRPC, self).__getattr__(name)
            return super(BaseRPC, self).__getattr__(name, default=default)
        except AttributeError:
            if not self.initialized:
                raise RuntimeError('You must connect or bind first'
                                   ' in order to call {!r}'.format(name))
            return AttributeWrapper(self, name=name)

    def send_to(self, user_id):
        return AttributeWrapper(self, user_id=user_id)

    def _setup_socket(self):
        if self.socket is None:
            self.socket = self.context.socket(self.socket_type)
        if self.routing_id:
            self.socket.identity = self.routing_id
        if self.socket_type == zmq.ROUTER:
            self.socket.ROUTER_MANDATORY = True
            if zmq.zmq_version_info() >= (4, 1, 0):
                self.socket.ROUTER_HANDOVER = True
        elif self.socket_type == zmq.REQ:
            self.socket.RCVTIMEO = int(self.timeout * 1000)
        self.socket.SNDTIMEO = int(self.timeout * 1000)
        self.auth_backend.configure()
        self.heartbeat_backend.configure()
        self.initialized = True

    def connect(self, endpoint):
        self._setup_socket()
        self.socket.connect(endpoint)

    def bind(self, endpoint):
        self._setup_socket()
        self.socket.bind(endpoint)

    def disconnect(self, endpoint):
        self.socket.disconnect(endpoint)

    def _prepare_work(self, user_id, name, *args, **kw):
        routing_id = self.auth_backend.get_routing_id(user_id)
        work = self.packer.packb((name, args, kw))
        uid = uuid.uuid4().bytes
        message = [routing_id, EMPTY_DELIMITER, VERSION, uid, WORK, work]
        return message, uid

    def create_timeout_detector(self, uuid):
        self.create_later_callback(functools.partial(self.timeout_task, uuid),
                                   self.timeout)

    def cleanup_future(self, uuid, future):
        try:
            del self.future_pool[uuid]
        except KeyError:
            pass

    def on_socket_ready(self, response):
        if len(response) == 4:
            # From REQ socket
            version, message_uuid, message_type = map(bytes, response[:-1])
            message = response[-1]
            routing_id = None
        else:
            # from ROUTER socket
            routing_id, delimiter, version, message_uuid, message_type = map(
                bytes, response[:-1])
            message = response[-1]
        try:
            user_id = message.get(b'User-Id').encode('utf-8')
        except zmq.ZMQError:
            # no zap handler
            user_id = b''
        else:
            self.auth_backend.register_routing_id(user_id, routing_id)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('Message received for {!r}: {!r} {}'.format(
                self.user_id,
                map(bytes, response[:-1]),
                pprint.pformat(
                    self.packer.unpackb(response[-1])
                    if message_type in (WORK, OK, HELLO)
                    else bytes(response[-1]))))
        assert version == VERSION
        if not self.auth_backend.is_authenticated(user_id):
            if message_type != HELLO:
                self.auth_backend.handle_authentication(user_id, routing_id,
                                                        message_uuid)
            else:
                self.auth_backend.handle_hello(user_id, routing_id,
                                               message_uuid, message)
        else:
            self.heartbeat_backend.handle_heartbeat(user_id, routing_id)
            if message_type == WORK:
                self._handle_work(message, routing_id, user_id, message_uuid)
            elif message_type == OK:
                return self._handle_ok(message, message_uuid)
            elif message_type == ERROR:
                self._handle_error(message, message_uuid)
            elif message_type == AUTHENTICATED:
                self.auth_backend.handle_authenticated(message)
            elif message_type == UNAUTHORIZED:
                self.auth_backend.handle_authentication(user_id, routing_id,
                                                        message_uuid)
            elif message_type == HELLO:
                self.auth_backend.handle_hello(user_id, routing_id,
                                               message_uuid, message)
            elif message_type == HEARTBEAT:
                # Can ignore, because every message is an heartbeat
                pass
            else:
                logger.error('Unknown message_type'
                             ' received {!r}'.format(message_type))
                raise NotImplementedError

    def _handle_work_proxy(self, locator, args, kw, user_id,
                           message_uuid):
        worker_callable = get_rpc_callable(
            locator,
            registry=self.registry,
            **self.auth_backend.get_predicate_arguments(user_id))
        if worker_callable.with_identity:
            return worker_callable(user_id, *args, **kw)
        return worker_callable(*args, **kw)

    def _handle_work(self, message, routing_id, user_id, message_uuid):
        locator, args, kw = self.packer.unpackb(message)
        try:
            try:
                result = self._handle_work_proxy(locator, args, kw, user_id,
                                                 message_uuid, )
            except ServiceNotFoundError:
                if self.proxy_to is None:
                    raise
                else:
                    result = self.proxy_to._handle_work_proxy(locator, args,
                                                              kw, user_id,
                                                              message_uuid)

        except Exception as error:
            logger.exception('Pseud job failed')
            traceback_ = traceback.format_exc()
            name = error.__class__.__name__
            message = str(error)
            result = (name, message, traceback_)
            status = ERROR
        else:
            status = OK
        response = self.packer.packb(result)
        message = [routing_id, EMPTY_DELIMITER, VERSION, message_uuid, status,
                   response]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('Worker send reply {!r} {!r}'.format(
                message[:-1],
                pprint.pformat(result))
            )
        self.send_message(message)

    def _handle_ok(self, message, message_uuid):
        value = self.packer.unpackb(message)
        logger.debug('Client result {!r} from {!r}'.format(value,
                                                           message_uuid))
        future = self.future_pool.pop(message_uuid)
        self._store_result_in_future(future, value)

    def _handle_error(self, message, message_uuid):
        value = self.packer.unpackb(message)
        future = self.future_pool.pop(message_uuid, DummyFuture())
        klass, message, traceback = value
        full_message = '\n'.join((format_remote_traceback(traceback),
                                  message))
        try:
            exception = getattr(builtins, klass)(full_message)
        except AttributeError:
            if klass in internal_exceptions:
                exception = getattr(interfaces, klass)(full_message)
                future.set_exception(exception)
            else:
                # Not stdlib Exception
                # fallback on something that expose informations received
                # from remote worker
                future.set_exception(Exception('\n'.join((klass,
                                                          full_message))))
        else:
            future.set_exception(exception)

    @property
    def register_rpc(self):
        return functools.partial(register_rpc, registry=self.registry)
