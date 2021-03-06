import logging
import os
import pprint
import uuid

from future import standard_library
import zmq
import zope.interface

with standard_library.hooks():
    import builtins

from . import auth, interfaces, heartbeat, predicate  # NOQA
from .common import (BaseRPC,
                     format_remote_traceback,
                     internal_exceptions,
                     )  # NOQA
from .interfaces import (IClient,
                         TimeoutError,
                         VERSION,
                         WORK,
                         )  # NOQA
try:
    from ._tornado import Client, Server
    if os.getenv('NO_TORNADO'):
        raise ImportError
except ImportError:
    from ._gevent import Client, Server  # NOQA


logger = logging.getLogger(__name__)


class SyncBaseRPC(BaseRPC):
    """
    Support limited features and run synchronously
    Doesn't require tornado nor gevent
    This is suitable to use in synchronous environment like
    within wsgi process
    """
    def _make_context(self):
        return zmq.Context.instance()

    def _backend_init(self, io_loop=None):
        self.reader = None
        self.internal_loop = False
        self.io_loop = None

    def send_work(self, peer_identity, name, *args, **kw):
        message, uid = self._prepare_work(name, *args, **kw)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('Sending work: {!r} {}'.format(
                message[:-1],
                pprint.pformat(self.packer.unpackb(message[-1]))))
        response = self.send_message(message)
        return response

    def _prepare_work(self, name, *args, **kw):
        work = self.packer.packb((name, args, kw))
        uid = uuid.uuid4().bytes
        message = [VERSION, uid, WORK, work]
        return message, uid

    def _handle_ok(self, message, message_uuid):
        value = self.packer.unpackb(message)
        logger.debug('SyncClient result {!r} from {!r}'.format(value,
                                                               message_uuid))
        return value

    def _handle_error(self, message, message_uuid):
        value = self.packer.unpackb(message)
        klass, message, traceback = value
        full_message = '\n'.join((format_remote_traceback(traceback),
                                  message))
        try:
            exception = getattr(builtins, klass)(full_message)
        except AttributeError:
            if klass in internal_exceptions:
                raise getattr(interfaces, klass)(full_message)
            else:
                # Not stdlib Exception
                # fallback on something that expose informations received
                # from remote worker
                raise Exception('\n'.join((klass, full_message)))
        else:
            raise exception

    def send_message(self, message):
        self.socket.send_multipart(message)
        try:
            response = self.socket.recv_multipart(copy=False)
        except zmq.Again:
            raise TimeoutError()
        return self.on_socket_ready(response)

    def _store_result_in_future(self, future, result):
        raise NotImplementedError('SyncClient can not do that')

    def start(self):
        pass

    def read_forever(self, socket, callback):
        raise NotImplementedError('SyncClient can not do that')

    def create_periodic_callback(self, callback, timer):
        raise NotImplementedError('SyncClient can not do that')

    def create_later_callback(self, callback, timer):
        raise NotImplementedError('SyncClient can not do that')

    def timeout_task(self, uuid):
        raise NotImplementedError('SyncClient can not do that')

    def stop(self):
        if not self.socket.closed:
            self.socket.linger = 0
            self.socket.close()
        self.auth_backend.stop()
        self.heartbeat_backend.stop()


@zope.interface.implementer(IClient)
class SyncClient(SyncBaseRPC):
    socket_type = zmq.REQ
