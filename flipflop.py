# flipflop - a FastCGI/WSGI gateway
#
# Copyright © 2005, 2006 Allan Saddi <allan@saddi.com>
# Copyright © 2013 Kozea <contact@kozea.fr>
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

"""
flipflop - a FastCGI/WSGI gateway.

For more information about FastCGI, see <http://www.fastcgi.com/>.

For more information about the Web Server Gateway Interface, see
<http://www.python.org/peps/pep-0333.html>.

Example usage:

  #!/usr/bin/env python
  from myapplication import app  # Assume app is your WSGI application object
  from flipflop import WSGIServer
  WSGIServer(app).run()

See the documentation for WSGIServer for more information.

"""

import os
import sys
import socket
import select
import signal
import struct
import errno
import threading
import fcntl
import resource
import traceback

# Constants from the spec.
FCGI_LISTENSOCK_FILENO = 0

FCGI_HEADER_LEN = 8

FCGI_VERSION_1 = 1

FCGI_BEGIN_REQUEST = 1
FCGI_ABORT_REQUEST = 2
FCGI_END_REQUEST = 3
FCGI_PARAMS = 4
FCGI_STDIN = 5
FCGI_STDOUT = 6
FCGI_STDERR = 7
FCGI_DATA = 8
FCGI_GET_VALUES = 9
FCGI_GET_VALUES_RESULT = 10
FCGI_UNKNOWN_TYPE = 11
FCGI_MAXTYPE = FCGI_UNKNOWN_TYPE

FCGI_NULL_REQUEST_ID = 0

FCGI_KEEP_CONN = 1

FCGI_RESPONDER = 1
FCGI_AUTHORIZER = 2
FCGI_FILTER = 3

FCGI_REQUEST_COMPLETE = 0
FCGI_CANT_MPX_CONN = 1
FCGI_OVERLOADED = 2
FCGI_UNKNOWN_ROLE = 3

FCGI_MAX_CONNS = 'FCGI_MAX_CONNS'
FCGI_MAX_REQS = 'FCGI_MAX_REQS'
FCGI_MPXS_CONNS = 'FCGI_MPXS_CONNS'

FCGI_Header = '!BBHHBx'
FCGI_BeginRequestBody = '!HB5x'
FCGI_EndRequestBody = '!LB3x'
FCGI_UnknownTypeBody = '!B7x'

FCGI_EndRequestBody_LEN = struct.calcsize(FCGI_EndRequestBody)
FCGI_UnknownTypeBody_LEN = struct.calcsize(FCGI_UnknownTypeBody)


class InputStream(object):
    """
    File-like object representing FastCGI input streams (FCGI_STDIN and
    FCGI_DATA). Supports the minimum methods required by WSGI spec.
    """
    # Limit the size of the InputStream's string buffer to this size + the
    # server's maximum Record size. Since the InputStream is not seekable,
    # we throw away already-read data once this certain amount has been read.
    threshold = 102400 - 8192

    def __init__(self, conn):
        self._conn = conn

        self._buf = b''
        self._bufList = []
        self._pos = 0  # Current read position.
        self._avail = 0  # Number of bytes currently available.

        self._eof = False  # True when server has sent EOF notification.

    def _shrinkBuffer(self):
        """Gets rid of already read data (since we can't rewind)."""
        if self._pos >= self.threshold:
            self._buf = self._buf[self._pos:]
            self._avail -= self._pos
            self._pos = 0

            assert self._avail >= 0

    def _waitForData(self):
        """Waits for more data to become available."""
        self._conn.process_input()

    def read(self, n=-1):
        if self._pos == self._avail and self._eof:
            return b''
        while True:
            if n < 0 or (self._avail - self._pos) < n:
                # Not enough data available.
                if self._eof:
                    # And there's no more coming.
                    newPos = self._avail
                    break
                else:
                    # Wait for more data.
                    self._waitForData()
                    continue
            else:
                newPos = self._pos + n
                break
        # Merge buffer list, if necessary.
        if self._bufList:
            self._buf += b''.join(self._bufList)
            self._bufList = []
        r = self._buf[self._pos:newPos]
        self._pos = newPos
        self._shrinkBuffer()
        return r

    def readline(self, length=None):
        if self._pos == self._avail and self._eof:
            return b''
        while True:
            # Unfortunately, we need to merge the buffer list early.
            if self._bufList:
                self._buf += b''.join(self._bufList)
                self._bufList = []
            # Find newline.
            i = self._buf.find(b'\n', self._pos)
            if i < 0:
                # Not found?
                if self._eof:
                    # No more data coming.
                    newPos = self._avail
                    break
                else:
                    if (length is not None and
                            len(self._buf) >= length + self._pos):
                        newPos = self._pos + length
                        break
                    # Wait for more to come.
                    self._waitForData()
                    continue
            else:
                newPos = i + 1
                break
        r = self._buf[self._pos:newPos]
        self._pos = newPos
        self._shrinkBuffer()
        return r

    def readlines(self, sizehint=0):
        total = 0
        lines = []
        line = self.readline()
        while line:
            lines.append(line)
            total += len(line)
            if 0 < sizehint <= total:
                break
            line = self.readline()
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        r = self.readline()
        if not r:
            raise StopIteration
        return r

    def add_data(self, data):
        if not data:
            self._eof = True
        else:
            self._bufList.append(data)
            self._avail += len(data)


class OutputStream(object):
    """
    FastCGI output stream (FCGI_STDOUT/FCGI_STDERR). By default, calls to
    write() or writelines() immediately result in Records being sent back
    to the server. Buffering should be done in a higher level!
    """
    def __init__(self, conn, req, type, buffered=False):
        self._conn = conn
        self._req = req
        self._type = type
        self._buffered = buffered
        self._bufList = []  # Used if buffered is True
        self.dataWritten = False
        self.closed = False

    def _write(self, data):
        length = len(data)
        while length:
            toWrite = min(length, self._req.server.maxwrite - FCGI_HEADER_LEN)

            rec = Record(self._type, self._req.requestId)
            rec.contentLength = toWrite
            rec.contentData = data[:toWrite]
            self._conn.writeRecord(rec)

            data = data[toWrite:]
            length -= toWrite

    def write(self, data):
        assert not self.closed

        if not data:
            return
        if type(data) == str:
            data = data.encode()

        self.dataWritten = True

        if self._buffered:
            self._bufList.append(data)
        else:
            self._write(data)

    def writelines(self, lines):
        assert not self.closed

        for line in lines:
            self.write(line)

    def flush(self):
        # Only need to flush if this OutputStream is actually buffered.
        if self._buffered:
            data = b''.join(self._bufList)
            self._bufList = []
            self._write(data)

    # Though available, the following should NOT be called by WSGI apps.
    def close(self):
        """Sends end-of-stream notification, if necessary."""
        if not self.closed and self.dataWritten:
            self.flush()
            rec = Record(self._type, self._req.requestId)
            self._conn.writeRecord(rec)
            self.closed = True


def decode_pair(s, pos=0):
    """
    Decodes a name/value pair.

    The number of bytes decoded as well as the name/value pair
    are returned.
    """
    nameLength = s[pos]
    if nameLength & 128:
        nameLength = struct.unpack('!L', s[pos:pos+4])[0] & 0x7fffffff
        pos += 4
    else:
        pos += 1

    valueLength = s[pos]
    if valueLength & 128:
        valueLength = struct.unpack('!L', s[pos:pos+4])[0] & 0x7fffffff
        pos += 4
    else:
        pos += 1

    name = s[pos:pos+nameLength]
    pos += nameLength
    value = s[pos:pos+valueLength]
    pos += valueLength

    return (pos, (name, value))


def encode_pair(name, value):
    """
    Encodes a name/value pair.

    The encoded string is returned.
    """
    nameLength = len(name)
    if nameLength < 128:
        s = bytes([nameLength])
    else:
        s = struct.pack('!L', nameLength | 0x80000000)

    valueLength = len(value)
    if valueLength < 128:
        s += bytes([valueLength])
    else:
        s += struct.pack('!L', valueLength | 0x80000000)

    return s + name + value


class Record(object):
    """
    A FastCGI Record.

    Used for encoding/decoding records.
    """
    def __init__(self, type=FCGI_UNKNOWN_TYPE, requestId=FCGI_NULL_REQUEST_ID):
        self.version = FCGI_VERSION_1
        self.type = type
        self.requestId = requestId
        self.contentLength = 0
        self.paddingLength = 0
        self.contentData = b''

    def _recvall(sock, length):
        """
        Attempts to receive length bytes from a socket, blocking if necessary.
        (Socket may be blocking or non-blocking.)
        """
        dataList = []
        recvLen = 0
        while length:
            try:
                data = sock.recv(length)
            except socket.error as e:
                if e.args[0] == errno.EAGAIN:
                    select.select([sock], [], [])
                    continue
                else:
                    raise
            if not data:  # EOF
                break
            dataList.append(data)
            dataLen = len(data)
            recvLen += dataLen
            length -= dataLen
        return b''.join(dataList), recvLen
    _recvall = staticmethod(_recvall)

    def read(self, sock):
        """Read and decode a Record from a socket."""
        try:
            header, length = self._recvall(sock, FCGI_HEADER_LEN)
        except:
            raise EOFError

        if length < FCGI_HEADER_LEN:
            raise EOFError

        self.version, self.type, self.requestId, self.contentLength, \
            self.paddingLength = struct.unpack(FCGI_Header, header)

        if self.contentLength:
            try:
                self.contentData, length = self._recvall(
                    sock, self.contentLength)
            except:
                raise EOFError

            if length < self.contentLength:
                raise EOFError

        if self.paddingLength:
            try:
                self._recvall(sock, self.paddingLength)
            except:
                raise EOFError

    @staticmethod
    def _sendall(sock, data):
        """
        Writes data to a socket and does not return until all the data is sent.
        """
        length = len(data)
        while length:
            try:
                sent = sock.send(data)
            except socket.error as e:
                if e.args[0] == errno.EAGAIN:
                    select.select([], [sock], [])
                    continue
                else:
                    raise
            data = data[sent:]
            length -= sent

    def write(self, sock):
        """Encode and write a Record to a socket."""
        self.paddingLength = -self.contentLength & 7

        header = struct.pack(
            FCGI_Header, self.version, self.type, self.requestId,
            self.contentLength, self.paddingLength)
        self._sendall(sock, header)
        if self.contentLength:
            self._sendall(sock, self.contentData)
        if self.paddingLength:
            self._sendall(sock, b'\x00' * self.paddingLength)


class Request(object):
    """
    Represents a single FastCGI request.

    These objects are passed to your handler and is the main interface
    between your handler and the fcgi module. The methods should not
    be called by your handler. However, server, params, stdin, stdout,
    stderr, and data are free for your handler's use.
    """
    def __init__(self, conn):
        self._conn = conn

        self.server = conn.server
        self.params = {}
        self.stdin = InputStream(conn)
        self.stdout = OutputStream(conn, self, FCGI_STDOUT)
        self.stderr = OutputStream(conn, self, FCGI_STDERR, buffered=True)
        self.data = InputStream(conn)

    def run(self):
        """Runs the handler, flushes the streams, and ends the request."""
        try:
            protocolStatus, appStatus = self.server.handler(self)
        except:
            traceback.print_exc(file=self.stderr)
            self.stderr.flush()
            if not self.stdout.dataWritten:
                self.server.error(self)

            protocolStatus, appStatus = FCGI_REQUEST_COMPLETE, 0

        try:
            self._flush()
            self._end(appStatus, protocolStatus)
        except socket.error as e:
            if e.args[0] != errno.EPIPE:
                raise

    def _end(self, appStatus=0, protocolStatus=FCGI_REQUEST_COMPLETE):
        self._conn.end_request(self, appStatus, protocolStatus)

    def _flush(self):
        self.stdout.close()
        self.stderr.close()


class Connection(object):
    """
    A Connection with the web server.

    Each Connection is associated with a single socket (which is
    connected to the web server) and is responsible for handling all
    the FastCGI message processing for that socket.
    """
    def __init__(self, sock, addr, server):
        self._sock = sock
        self._addr = addr
        self.server = server

        # Active Requests for this Connection, mapped by request ID.
        self._requests = {}

    def _cleanupSocket(self):
        """Close the Connection's socket."""
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except:
            return
        try:
            while True:
                r, w, e = select.select([self._sock], [], [])
                if not r or not self._sock.recv(1024):
                    break
        except:
            pass
        self._sock.close()

    def run(self):
        """Begin processing data from the socket."""
        self._keepGoing = True
        while self._keepGoing:
            try:
                self.process_input()
            except (EOFError, KeyboardInterrupt):
                break
            except (select.error, socket.error) as e:
                if e.args[0] == errno.EBADF:  # Socket was closed by Request.
                    break
                raise

        self._cleanupSocket()

    def process_input(self):
        """Attempt to read a single Record from the socket and process it."""
        # Currently, any children Request threads notify this Connection
        # that it is no longer needed by closing the Connection's socket.
        # We need to put a timeout on select, otherwise we might get
        # stuck in it indefinitely... (I don't like this solution.)
        while self._keepGoing:
            try:
                r, w, e = select.select([self._sock], [], [], 1.0)
            except ValueError:
                # Sigh. ValueError gets thrown sometimes when passing select
                # a closed socket.
                raise EOFError
            if r:
                break
        if not self._keepGoing:
            return
        rec = Record()
        rec.read(self._sock)

        if rec.type == FCGI_GET_VALUES:
            self._do_get_values(rec)
        elif rec.type == FCGI_BEGIN_REQUEST:
            self._do_begin_request(rec)
        elif rec.type == FCGI_ABORT_REQUEST:
            self._do_abort_request(rec)
        elif rec.type == FCGI_PARAMS:
            self._do_params(rec)
        elif rec.type == FCGI_STDIN:
            self._do_stdin(rec)
        elif rec.type == FCGI_DATA:
            self._do_data(rec)
        elif rec.requestId == FCGI_NULL_REQUEST_ID:
            self._do_unknown_type(rec)
        else:
            # Need to complain about this.
            pass

    def writeRecord(self, rec):
        """
        Write a Record to the socket.
        """
        rec.write(self._sock)

    def end_request(self, req, appStatus=0,
                    protocolStatus=FCGI_REQUEST_COMPLETE, remove=True):
        """
        End a Request.

        Called by Request objects. An FCGI_END_REQUEST Record is
        sent to the web server. If the web server no longer requires
        the connection, the socket is closed, thereby ending this
        Connection (run() returns).
        """
        rec = Record(FCGI_END_REQUEST, req.requestId)
        rec.contentData = struct.pack(FCGI_EndRequestBody, appStatus,
                                      protocolStatus)
        rec.contentLength = FCGI_EndRequestBody_LEN
        self.writeRecord(rec)

        if remove:
            del self._requests[req.requestId]

        if not (req.flags & FCGI_KEEP_CONN) and not self._requests:
            self._cleanupSocket()
            self._keepGoing = False

    def _do_get_values(self, inrec):
        """Handle an FCGI_GET_VALUES request from the web server."""
        outrec = Record(FCGI_GET_VALUES_RESULT)

        pos = 0
        while pos < inrec.contentLength:
            pos, (name, value) = decode_pair(inrec.contentData, pos)
            cap = self.server.capability.get(name)
            if cap is not None:
                outrec.contentData += encode_pair(
                    name, str(cap).encode('latin-1'))

        outrec.contentLength = len(outrec.contentData)
        self.writeRecord(outrec)

    def _do_begin_request(self, inrec):
        """Handle an FCGI_BEGIN_REQUEST from the web server."""
        role, flags = struct.unpack(FCGI_BeginRequestBody, inrec.contentData)

        req = Request(self)

        req.requestId, req.role, req.flags = inrec.requestId, role, flags
        req.aborted = False

        if self._requests:
            self.end_request(req, 0, FCGI_CANT_MPX_CONN, remove=False)
        else:
            self._requests[inrec.requestId] = req

    def _do_abort_request(self, inrec):
        """
        Handle an FCGI_ABORT_REQUEST from the web server.

        We just mark a flag in the associated Request.
        """
        req = self._requests.get(inrec.requestId)
        if req is not None:
            req.aborted = True

    def _start_request(self, req):
        """Run the request."""
        req.run()

    def _do_params(self, inrec):
        """
        Handle an FCGI_PARAMS Record.

        If the last FCGI_PARAMS Record is received, start the request.
        """
        req = self._requests.get(inrec.requestId)
        if req is not None:
            if inrec.contentLength:
                pos = 0
                while pos < inrec.contentLength:
                    pos, (name, value) = decode_pair(inrec.contentData, pos)
                    req.params[name.decode('latin-1')] = \
                        value.decode('latin-1')
            else:
                self._start_request(req)

    def _do_stdin(self, inrec):
        """Handle the FCGI_STDIN stream."""
        req = self._requests.get(inrec.requestId)
        if req is not None:
            req.stdin.add_data(inrec.contentData)

    def _do_data(self, inrec):
        """Handle the FCGI_DATA stream."""
        req = self._requests.get(inrec.requestId)
        if req is not None:
            req.data.add_data(inrec.contentData)

    def _do_unknown_type(self, inrec):
        """Handle an unknown request type. Respond accordingly."""
        outrec = Record(FCGI_UNKNOWN_TYPE)
        outrec.contentData = struct.pack(FCGI_UnknownTypeBody, inrec.type)
        outrec.contentLength = FCGI_UnknownTypeBody_LEN
        self.writeRecord(outrec)


class ThreadPool(object):
    """
    Thread pool that maintains the number of idle threads between
    minSpare and maxSpare inclusive. By default, there is no limit on
    the number of threads that can be started, but this can be controlled
    by maxThreads.
    """
    def __init__(self, minSpare=1, maxSpare=5, maxThreads=sys.maxsize):
        self._minSpare = minSpare
        self._maxSpare = maxSpare
        self._maxThreads = max(minSpare, maxThreads)

        self._lock = threading.Condition()
        self._workQueue = []
        self._idleCount = self._workerCount = maxSpare

        self._threads = []
        self._stop = False

        # Start the minimum number of worker threads.
        for i in range(maxSpare):
            self._start_new_thread()

    def _start_new_thread(self):
        t = threading.Thread(target=self._worker)
        self._threads.append(t)
        t.setDaemon(True)
        t.start()
        return t

    def shutdown(self):
        """shutdown all workers."""
        self._lock.acquire()
        self._stop = True
        self._lock.notifyAll()
        self._lock.release()

        # wait for all threads to finish
        for t in self._threads[:]:
            t.join()

    def addJob(self, job, allowQueuing=True):
        """
        Adds a job to the work queue. The job object should have a run()
        method. If allowQueuing is True (the default), the job will be
        added to the work queue regardless if there are any idle threads
        ready. (The only way for there to be no idle threads is if maxThreads
        is some reasonable, finite limit.)

        Otherwise, if allowQueuing is False, and there are no more idle
        threads, the job will not be queued.

        Returns True if the job was queued, False otherwise.
        """
        self._lock.acquire()
        try:
            # Maintain minimum number of spares.
            while (self._idleCount < self._minSpare and
                   self._workerCount < self._maxThreads):
                try:
                    self._start_new_thread()
                except:
                    return False
                self._workerCount += 1
                self._idleCount += 1

            # Hand off the job.
            if self._idleCount or allowQueuing:
                self._workQueue.append(job)
                self._lock.notify()
                return True
            else:
                return False
        finally:
            self._lock.release()

    def _worker(self):
        """
        Worker thread routine. Waits for a job, executes it, repeat.
        """
        self._lock.acquire()
        try:
            while True:
                while not self._workQueue and not self._stop:
                    self._lock.wait()

                if self._stop:
                    return

                # We have a job to do...
                job = self._workQueue.pop(0)

                assert self._idleCount > 0
                self._idleCount -= 1

                self._lock.release()

                try:
                    job.run()
                except:
                    # FIXME: This should really be reported somewhere.
                    # But we can't simply report it to stderr because of fcgi
                    pass

                self._lock.acquire()

                if self._idleCount == self._maxSpare:
                    break  # NB: lock still held
                self._idleCount += 1
                assert self._idleCount <= self._maxSpare

            # Die off...
            assert self._workerCount > self._maxSpare
            self._threads.remove(threading.currentThread())
            self._workerCount -= 1
        finally:
            self._lock.release()


class WSGIServer(object):
    """
    FastCGI server that supports the Web Server Gateway Interface. See
    <http://www.python.org/peps/pep-0333.html>.
    """
    # The maximum number of bytes (per Record) to write to the server.
    # I've noticed mod_fastcgi has a relatively small receive buffer (8K or
    # so).
    maxwrite = 8192

    def __init__(self, application):
        self.application = application
        self.environ = {}
        max_connections = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        self.capability = {
            FCGI_MAX_CONNS: max_connections,
            FCGI_MAX_REQS: max_connections,
            FCGI_MPXS_CONNS: 0}
        self._threadPool = ThreadPool()

    def shutdown(self):
        """Wait for running threads to finish."""
        self._threadPool.shutdown()

    def _exit(self, reload=False):
        """
        Protected convenience method for subclasses to force an exit. Not
        really thread-safe, which is why it isn't public.
        """
        if self._keepGoing:
            self._keepGoing = False
            self._hupReceived = reload

    # Signal handlers

    def _hupHandler(self, signum, frame):
        self._hupReceived = True
        self._keepGoing = False

    def _intHandler(self, signum, frame):
        self._keepGoing = False

    def _installSignalHandlers(self):
        supportedSignals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, 'SIGHUP'):
            supportedSignals.append(signal.SIGHUP)

        self._oldSIGs = [(x, signal.getsignal(x)) for x in supportedSignals]

        for sig in supportedSignals:
            if hasattr(signal, 'SIGHUP') and sig == signal.SIGHUP:
                signal.signal(sig, self._hupHandler)
            else:
                signal.signal(sig, self._intHandler)

    def _restoreSignalHandlers(self):
        for signum, handler in self._oldSIGs:
            signal.signal(signum, handler)

    def handler(self, req):
        """Special handler for WSGI."""
        if req.role != FCGI_RESPONDER:
            return FCGI_UNKNOWN_ROLE, 0

        # Mostly taken from example CGI gateway.
        environ = req.params
        environ.update(self.environ)

        environ['wsgi.version'] = (1, 0)
        environ['wsgi.input'] = req.stdin
        environ['wsgi.errors'] = req.stderr
        environ['wsgi.multithread'] = True
        environ['wsgi.multiprocess'] = False
        environ['wsgi.run_once'] = False

        if environ.get('HTTPS', 'off') in ('on', '1'):
            environ['wsgi.url_scheme'] = 'https'
        else:
            environ['wsgi.url_scheme'] = 'http'

        self._sanitizeEnv(environ)

        headers_set = []
        headers_sent = []
        result = None

        def write(data):
            if type(data) is str:
                data = data.encode('latin-1')

            assert type(data) is bytes, 'write() argument must be bytes'
            assert headers_set, 'write() before start_response()'

            if not headers_sent:
                status, responseHeaders = headers_sent[:] = headers_set
                found = False
                for header, value in responseHeaders:
                    if header.lower() == b'content-length':
                        found = True
                        break
                if not found and result is not None:
                    try:
                        if len(result) == 1:
                            responseHeaders.append((
                                b'Content-Length',
                                str(len(data)).encode('latin-1')))
                    except:
                        pass
                s = b'Status: ' + status + b'\r\n'
                for header, value in responseHeaders:
                    s += header + b': ' + value + b'\r\n'
                s += b'\r\n'
                req.stdout.write(s)

            req.stdout.write(data)
            req.stdout.flush()

        def start_response(status, response_headers, exc_info=None):
            if exc_info:
                try:
                    if headers_sent:
                        # Re-raise if too late
                        info = exc_info[0](exc_info[1])
                        raise info.with_traceback(exc_info[2])
                finally:
                    exc_info = None  # avoid dangling circular ref
            else:
                assert not headers_set, 'Headers already set!'

            if type(status) is str:
                status = status.encode('latin-1')

            assert type(status) is bytes, 'Status must be a string'
            assert len(status) >= 4, 'Status must be at least 4 characters'
            assert int(status[:3]), 'Status must begin with 3-digit code'
            assert status[3] == 0x20, 'Status must have a space after code'
            assert type(response_headers) is list, 'Headers must be a list'
            new_response_headers = []
            for name, val in response_headers:
                if type(name) is str:
                    name = name.encode('latin-1')
                if type(val) is str:
                    val = val.encode('latin-1')

                assert type(name) is bytes, (
                    'Header name "%s" must be bytes' % name)
                assert type(val) is bytes, (
                    'Value of header "%s" must be bytes' % name)

                new_response_headers.append((name, val))

            headers_set[:] = [status, new_response_headers]
            return write

        try:
            result = self.application(environ, start_response)
            try:
                for data in result:
                    if data:
                        write(data)
                if not headers_sent:
                    write(b'')  # in case body was empty
            finally:
                if hasattr(result, 'close'):
                    result.close()
        except socket.error as e:
            if e.args[0] != errno.EPIPE:
                raise  # Don't let EPIPE propagate beyond server

        return FCGI_REQUEST_COMPLETE, 0

    def _sanitizeEnv(self, environ):
        """Ensure certain values are present, if required by WSGI."""
        if 'SCRIPT_NAME' not in environ:
            environ['SCRIPT_NAME'] = ''

        reqUri = None
        if 'REQUEST_URI' in environ:
            reqUri = environ['REQUEST_URI'].split('?', 1)

        if 'PATH_INFO' not in environ or not environ['PATH_INFO']:
            if reqUri is not None:
                scriptName = environ['SCRIPT_NAME']
                if not reqUri[0].startswith(scriptName):
                    environ['wsgi.errors'].write(
                        'WARNING: SCRIPT_NAME does not match REQUEST_URI')
                environ['PATH_INFO'] = reqUri[0][len(scriptName):]
            else:
                environ['PATH_INFO'] = ''
        if 'QUERY_STRING' not in environ or not environ['QUERY_STRING']:
            if reqUri is not None and len(reqUri) > 1:
                environ['QUERY_STRING'] = reqUri[1]
            else:
                environ['QUERY_STRING'] = ''

        # If any of these are missing, it probably signifies a broken
        # server...
        for name, default in [('REQUEST_METHOD', 'GET'),
                              ('SERVER_NAME', 'localhost'),
                              ('SERVER_PORT', '80'),
                              ('SERVER_PROTOCOL', 'HTTP/1.0')]:
            if name not in environ:
                environ['wsgi.errors'].write(
                    '%s: missing FastCGI param %s required by WSGI!\n' %
                    (self.__class__.__name__, name))
                environ[name] = default

    def error(self, req):
        """
        Called by Request if an exception occurs within the handler. May and
        should be overridden.
        """
        req.stdout.write(
            b'Status: 500 Internal Server Error\r\n'
            b'Content-Type: text/html\r\n\r\n'
            b'<!doctype html>\r\n'
            b'<title>Unhandled Exception</title>\r\n'
            b'<h1>Unhandled Exception</h1>\r\n'
            b'An unhandled exception was thrown by the application.\r\n')

    def run(self):
        """
        The main loop. Exits on SIGHUP, SIGINT, SIGTERM. Returns True if
        SIGHUP was received, False otherwise.
        """
        self._web_server_addrs = os.environ.get('FCGI_WEB_SERVER_ADDRS')
        if self._web_server_addrs is not None:
            self._web_server_addrs = [
                x.strip() for x in self._web_server_addrs.split(',')]

        sock = socket.fromfd(
            FCGI_LISTENSOCK_FILENO, socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.getpeername()
        except socket.error as e:
            if e.args[0] != errno.ENOTCONN:
                raise

        # Set up signal handlers.
        self._keepGoing = True
        self._hupReceived = False

        # Might need to revisit this?
        if not sys.platform.startswith('win'):
            self._installSignalHandlers()

        # Set close-on-exec
        fcntl.fcntl(sock.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)

        # Main loop.
        while self._keepGoing:
            try:
                r, w, e = select.select([sock], [], [], 1.0)
            except select.error as e:
                if e.args[0] == errno.EINTR:
                    continue
                raise

            if r:
                try:
                    clientSock, addr = sock.accept()
                except socket.error as e:
                    if e.args[0] in (errno.EINTR, errno.EAGAIN):
                        continue
                    raise

                fcntl.fcntl(
                    clientSock.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)

                if not (self._web_server_addrs is None or
                        (len(addr) == 2 and addr[0] in self._web_server_addrs)):
                    clientSock.close()
                    continue

                # Hand off to Connection.
                conn = Connection(clientSock, addr, self)
                if not self._threadPool.addJob(conn, allowQueuing=False):
                    # No thread left, immediately close the socket to hopefully
                    # indicate to the web server that we're at our limit...
                    # and to prevent having too many opened (and useless)
                    # files.
                    clientSock.close()

        self._restoreSignalHandlers()

        # Return bool based on whether or not SIGHUP was received.
        sock.close()
        self.shutdown()

        return self._hupReceived
