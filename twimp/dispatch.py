#   Copyright (c) 2011  Arek Korbik
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from collections import deque
import logging
import time

from twisted.internet import protocol
from twisted.internet import defer, reactor
from twisted.python import failure

from twimp import amf0
from twimp import chunks
from twimp import const
from twimp.error import ProtocolContractError, UnexpectedStatusError
from twimp.error import CommandResultError
from twimp.error import CallResultError, CallAbortedException
from twimp.proto import DispatchProtocol, DispatchFactory
from twimp.utils import ms_time_wrapped

LOG_CATEGORY = 'dispatch'
import twimp.log
log = twimp.log.get_logger(LOG_CATEGORY)

# defer.Deferred.debug = 1

class SharedObject(object):
    def __init__(self, name, persistance):
        self._name = name
        self._readyd = defer.Deferred()
        self._deleted = defer.Deferred()
        self._changed = defer.Deferred()
        self._persistance = persistance
        self._dispatcher = {
            const.SO_EVENT_TYPE_CHANGE: self.onChangeRaw,
            const.SO_EVENT_TYPE_CLEAR: self.onClear,
            const.SO_EVENT_TYPE_MESSAGE: self.onMessage,
            const.SO_EVENT_TYPE_DELETE: self.onDeleteRaw,
            const.SO_EVENT_TYPE_USE_SUCCESS: self.onUseSuccess,
        }

    def onEvent(self, event):
        #log.debug("shared object %s event: %s" % (self.name, event))
        handler = self._dispatcher.get(event['type'], None)
        if handler is None:
            return self.unhandledEvent(event)

        return handler(event['data'])

    def unhandledEvent(self, event):
        log.warning("SharedObject %s cannot parse event %s" % (self.name, event))

    def onChangeRaw(self, event):
        try:
            change_dict = amf0.decode_so_event_change_data(event)
        except amf0.DecoderError as e:
            log.warning("Failed to decode event data: %s" % e)
            return

        key, value = change_dict.items()[0]
        return self.onChange(key, value)

    def onChange(self, key_name, value):
        log.debug("onChange called: %s=%s" % (key_name, value))
        d, self._changed = self._changed, defer.Deferred()
        d.callback((self._changed, key_name, value))

    def onClear(self, event):
        assert len(event) == 0, "Event data of clear must be empty"
        # Nothing to do really.

    def onDeleteRaw(self, event):
        assert len(event) != 0, "Cannot call onDelete without data"
        self.onDelete(amf0._decode_string(event))

    def onDelete(self, key_name):
        log.debug("onDelete called for %s" % key_name)
        d, self._deleted = self._deleted, defer.Deferred()
        d.callback((self._deleted, key_name))

    def onMessage(self, event):
        # TODO: Handle if needed
        pass

    def onUseSuccess(self, event):
        assert len(event) == 0, "Event data of success must be empty"
        self._readyd.callback(self)

    @property
    def name(self):
        return self._name

    @property
    def readyd(self):
        return self._readyd

    @property
    def deleted(self):
        return self._deleted

    @property
    def changed(self):
        return self._changed

    @property
    def persistance(self):
        return self._persistance

class CancellableCallQueue(object):
    def __init__(self, reactor=reactor):
        self.reactor = reactor
        self.pending = {}
        self._next_key = 0

    def callLater(self, delay, f, *args, **kw):
        call_key, self._next_key = self._next_key, self._next_key + 1
        clid = self.reactor.callLater(delay, self._call_wrapper, call_key, f,
                                      args, kw)
        self.pending[call_key] = clid
        return call_key, clid

    def cancel(self, call_key, clid):
        self.pending.pop(call_key, None)
        return clid.cancel()

    def _call_wrapper(self, call_key, f, args, kw):
        del self.pending[call_key]
        f(*args, **kw)

    def cancel_all(self):
        remaining = self.pending.copy()
        self.pending.clear()

        for key, clid  in remaining.iteritems():
            if clid.active():
                clid.cancel()

class DeferredTracker(object):
    init_trans_id = 1

    def __init__(self):
        self._pending = {}
        self._next_trans_id = {}

    def next_trans(self, key):
        trans_id = self._next_trans_id.setdefault(key, self.init_trans_id)
        r, trans_id = trans_id, trans_id + 1
        self._next_trans_id[key] = trans_id
        return r

    def push_deferred(self, key, trans_id, d):
        if key not in self._pending:
            self._pending[key] = {}
        key_queue = self._pending[key]
        key_queue[trans_id] = d

    def pop_deferred(self, key, trans_id):
        d = None
        key_queue = self._pending.get(key, None)
        if key_queue:
            d = key_queue.pop(trans_id, None)
        return d

    def iter_all(self):
        return ((key, d)
                for (key, key_queue) in self._pending.iteritems()
                for d in key_queue.itervalues())

    def clear(self):
        self._pending = {}

    def reset(self):
        self._next_trans_id = {}

class StatusEventTracker(object):
    def __init__(self):
        # { key => deque([(code, Deferred), ...]) }
        self._event_callbacks = {}

    def add(self, key, code, d):
        q = self._event_callbacks.get(key, None)
        if q is None:
            q = self._event_callbacks[key] = deque()
        q.append((code, d))

    def pop(self, key):
        code, d = None, None
        q = self._event_callbacks.get(key, None)
        if q:
            code, d = q.popleft()
        return code, d

    def pop_all(self):
        ret = ((code, d)
               for q in self._event_callbacks.itervalues()
               for (code, d) in q)
        self._event_callbacks = {}
        return ret

    def wait(self, key, code):
        d = defer.Deferred()
        self.add(key, code, d)
        return d

    def cancel_all(self, reason=None):
        waiting = self.pop_all()
        if reason:
            for _code, d in waiting:
                d.errback(reason)

    def dispatch(self, key, info, miss_h=None):
        code, d = self.pop(key)

        if not d:
            if miss_h:
                miss_h()
        else:
            try:
                evt_code = info.code
            except AttributeError, e:
                d.errback(ProtocolContractError(e))
            else:
                if code is None or evt_code == code:
                    d.callback(info)
                else:
                    d.errback(UnexpectedStatusError(info))

class CommandDispatchProtocol(DispatchProtocol):

    def __init__(self):
        DispatchProtocol.__init__(self)

        self._cc_queue = CancellableCallQueue()
        self._call_tracker = DeferredTracker()
        self._shared_objs = dict()

    def doCommand(self, ts, ms_id, args):
        cmd = args[0]

        handler_m = getattr(self, 'command_%s' % (cmd,), None)

        if handler_m is None:
            self._cc_queue.callLater(0, self.unknownCommandType, cmd, ts,
                                     ms_id, args[1:])
        else:
            self._cc_queue.callLater(0, self._handler_wrapper, handler_m,
                                     ts, ms_id, args[1:])

    def doSharedObj(self, ts, ms_id, obj_name, events):
        if not self._shared_objs.has_key(obj_name):
            self.unknownSharedObject(ts, ms_id, obj_name, events)
            return

        so = self._shared_objs[obj_name]
        for e in events:
            so.onEvent(e)

    def _handler_wrapper(self, handler, ts, ms_id, args):
        # wrap in try/except...?
        handler(ts, ms_id, *args)

    def command__result(self, ts, ms_id, trans_id, *args):
        d = self._call_tracker.pop_deferred(ms_id, trans_id)

        if d:
            d.callback(args)
        else:
            self.unexpectedCallResult(ts, ms_id, trans_id, args)

    def command__error(self, ts, ms_id, trans_id, *args):
        d = self._call_tracker.pop_deferred(ms_id, trans_id)

        if d:
            d.errback(failure.Failure(CommandResultError(*args)))
        else:
            self.unexpectedCallError(ts, ms_id, trans_id, args)

    def unknownCommandType(self, cmd, ts, msid, args):
        raise NotImplementedError('unknown command %r%r' % (cmd,
                                                            (ts, msid, args)))
    def unknownSharedObject(self, ts, ms_id, obj_name, events):
        log.warning('unexpected shared object event: %s => %s' % (obj_name, events))

    def unexpectedCallResult(self, ts, ms_id, trans_id, args):
        log.warning('unexpected _result: at %r, stream %r, trans %r, args: %r',
                    ts, ms_id, trans_id, args)

    def unexpectedCallError(self, ts, ms_id, trans_id, args):
        log.warning('unexpected _error: at %r, stream %r, trans %r, args: %r',
                    ts, ms_id, trans_id, args)

    def connectionLost(self, reason=protocol.connectionDone):
        self._cc_queue.cancel_all()
        pending_calls = list(self._call_tracker.iter_all())
        self._call_tracker.clear()
        for ms_id, d in pending_calls:
            d.errback(reason)
        DispatchProtocol.connectionLost(self, reason)

    def encode_amf(self, *args):
        # for now only supporting AMF0
        return amf0.encode(*args)

    def _send_command(self, ts, ms_id, body, track_id):
        # track_id > 0 -> will return a tracking deferred
        d = None

        if track_id:
            d = defer.Deferred()
            self._call_tracker.push_deferred(ms_id, track_id, d)

        self.muxer.sendMessage(0, chunks.MSG_COMMAND, ms_id, body)
        return d

    def _sendRemote(self, ms_id, cmd, args, kwargs, track):
        # ignoring kwargs for now...
        trans_id = 0
        if track:
            trans_id = self._call_tracker.next_trans(ms_id)
        encoded_args = self.encode_amf(cmd, trans_id, *args)

        # hardcoding 0 time, does not seem to matter much...
        return self._send_command(0, ms_id, encoded_args, trans_id)

    def callRemote(self, ms_id, cmd, *args, **kw):
        return self._sendRemote(ms_id, cmd, args, kw, True)

    def signalRemote(self, ms_id, cmd, *args, **kw):
        # similar to callRemote, except we don't expect any results
        return self._sendRemote(ms_id, cmd, args, kw, False)

    @defer.inlineCallbacks
    def useSharedObject(self, ms_id, obj_name, persistance=False):
        if self._shared_objs.has_key(obj_name):
            raise SystemError("Shared object %s already in use" % obj_name)

        events = [{'data': '', 'type': const.SO_EVENT_TYPE_USE}]
        flags='\x00\x00\x00\x00\x00\x00\x00\x00'
        if persistance:
            flags = '\x00\x00\x00\x02\x00\x00\x00\x00'
        body = amf0.encode_so_update(obj_name, flags=flags, events=events)
        so = SharedObject(obj_name, persistance)
        self._shared_objs[obj_name] = so

        ts = ms_time_wrapped(self.session_time())
        yield self.muxer.sendMessage(ts, chunks.MSG_SO, ms_id, body)
        defer.returnValue((yield so.readyd))

    @defer.inlineCallbacks
    def releaseSharedObject(self, ms_id, obj_name):
        if not self._shared_objs.has_key(obj_name):
            raise SystemError("Shared object %s does not exists" % obj_name)

        so = self._shared_objs[obj_name]
        events = [{'data': '', 'type': const.SO_EVENT_TYPE_RELEASE}]
        flags='\x00\x00\x00\x00\x00\x00\x00\x00'
        if so.persistance:
            flags = '\x00\x00\x00\x02\x00\x00\x00\x00'
        body = amf0.encode_so_update(obj_name, flags=flags, events=events)
        del self._shared_objs[obj_name]

        ts = ms_time_wrapped(self.session_time())
        yield self.muxer.sendMessage(ts, chunks.MSG_SO, ms_id, body)

        defer.returnValue(None)

class CommandDispatchFactory(DispatchFactory):
    protocol = CommandDispatchProtocol

class EventDispatchProtocol(CommandDispatchProtocol):
    def __init__(self):
        CommandDispatchProtocol.__init__(self)

        self._events = StatusEventTracker()

    def _onStatus_ev_key(self, ms_id):
        return (None, ms_id)

    def waitStatus(self, ms_id, code):
        return self._events.wait(self._onStatus_ev_key(ms_id), code)

    def command_onStatus(self, ts, ms_id, _trans_id, _none, info):
        # trans_id not used, and _none seems to always be None...

        def miss_handler():
            self.unhandledOnStatus(ts, ms_id, info)

        self._events.dispatch(self._onStatus_ev_key(ms_id), info,
                              miss_h=miss_handler)

    def unhandledOnStatus(self, ts, ms_id, info):
        log.warning('unhandled onStatus: at %r, stream %r, info: %r',
                    ts, ms_id, info)

    def connectionLost(self, reason=protocol.connectionDone):
        self._events.cancel_all(reason=reason)

        CommandDispatchProtocol.connectionLost(self, reason)

class EventDispatchFactory(CommandDispatchFactory):
    protocol = EventDispatchProtocol

class CallDispatchProtocol(EventDispatchProtocol):
    def __init__(self):
        EventDispatchProtocol.__init__(self)

    def session_time(self):
        return time.time() - self.session_init_time

    def unknownCommandType(self, cmd, ts, ms_id, args):
        trans_id = args[0]

        handler_m = getattr(self, 'remote_%s' % (cmd,), None)

        if handler_m is None:
            d = defer.maybeDeferred(self.unknownRemoteCall, cmd, ts, ms_id,
                                    args[1:])
        else:
            d = defer.maybeDeferred(handler_m, ts, ms_id, *args[1:])

        if trans_id:
            d.addCallback(self._remote_handler_cb, ms_id, trans_id)

        d.addErrback(self._remote_abort_handler_eb)
        d.addErrback(self._remote_handler_eb, ms_id, trans_id)

    def _remote_abort_handler_eb(self, failure):
        failure.trap(CallAbortedException)
        # log failure but do nothing more
        log.debug('remote call aborted: %s', failure.value)

    def _remote_handler_cb(self, result, ms_id, trans_id):
        # log.debug('remote call result: %r', result)
        if not isinstance(result, (tuple, list)):
            result = (result,)

        body = self.encode_amf('_result', trans_id, *result)

        ts = ms_time_wrapped(self.session_time())
        self.muxer.sendMessage(ts, chunks.MSG_COMMAND, ms_id, body)

    def _remote_handler_eb(self, failure, ms_id, trans_id):
        if log.isEnabledFor(logging.DEBUG):
            log.info('remote call failure: %s', failure.value,
                     exc_info=(failure.type, failure.value,
                               failure.getTracebackObject()))
        else:
            log.info('remote call failure: %s', failure.value)

        fatal = False
        if failure.check(CallResultError):
            body = self.encode_amf('_error', trans_id,
                                   *failure.value.get_error_args())
            fatal = failure.value.is_fatal
        else:
            err = amf0.Object(code='NetStream.Failed', level='error',
                              description=repr(failure.value))
            body = self.encode_amf('_error', trans_id, None, err)

        ts = ms_time_wrapped(self.session_time())
        self.muxer.sendMessage(ts, chunks.MSG_COMMAND, ms_id, body)

        if fatal:
            self.transport.loseConnection()

    def unknownRemoteCall(self, cmd, ts, ms_id, args):
        # seems that we're just supposed to silently ignore the request
        log.warning('unknown method called: %s, args: %r', cmd, args)
        raise CallAbortedException('unknown command %r' % (cmd,))

class CallDispatchFactory(EventDispatchFactory):
    protocol = CallDispatchProtocol
