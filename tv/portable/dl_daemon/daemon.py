from dl_daemon import command
import os
import cPickle
import socket
import traceback
from threading import Lock, Thread, Event
from time import sleep
from struct import pack, unpack, calcsize
import tempfile
import eventloop

SIZE_OF_INT = calcsize("I")

class DaemonError(Exception):
    """Exception while communicating to a daemon (either controller or
    downloader).
    """
    pass

firstDaemonLaunch = '1'
def launchDownloadDaemon(oldpid, port):
    global firstDaemonLaunch

    daemonEnv = {
        'DEMOCRACY_DOWNLOADER_PORT' : str(port),
        'DEMOCRACY_DOWNLOADER_FIRST_LAUNCH' : firstDaemonLaunch,
    }
    import app
    delegate = app.controller.getBackendDelegate()
    delegate.launchDownloadDaemon(oldpid, daemonEnv)
    firstDaemonLaunch = '0'

def getDataFile():
    try:
        uid = os.getuid()
    except:
        # This works for win32, where we don't have getuid()
        uid = os.environ['USERNAME']
        
    return os.path.join(tempfile.gettempdir(), 'Democracy_Download_Daemon_%s.txt' % uid)

pidfile = None
def writePid(pid):
    """Write out our pid.

    This method locks the pid file until the downloader exits.  On windows
    this is achieved by keeping the file open.  On Unix/OS X, we use the
    fcntl.lockf() function.
    """

    global pidfile
    # NOTE: we want to open the file in a mode the standard open() doesn't
    # support.  We want to create the file if nessecary, but not truncate it
    # if it's already around.  We can't truncate it because on unix we haven't
    # locked the file yet.
    fd = os.open(getDataFile(), os.O_WRONLY | os.O_CREAT)
    pidfile = os.fdopen(fd, 'w')
    try:
        import fcntl
    except:
        pass
    else:
        fcntl.lockf(pidfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pidfile.write("%s\n" % pid)
    pidfile.flush()
    # NOTE: There may be extra data after the line we write left around from
    # prevous writes to the pid file.  This is fine since readPid() only reads
    # the 1st line.
    #
    # NOTE 2: we purposely don't close the file, to achieve locking on
    # windows.

def readPid():
    try:
        f = open(getDataFile(), "r")
    except IOError:
        return None
    try:
        try:
            return int(f.readline())
        except ValueError:
            return None
    finally:
        f.close()

lastDaemon = None

class Daemon:
    def __init__(self):
        global lastDaemon
        lastDaemon = self
        self.waitingCommands = {}
        self.returnValues = {}
        self.sendLock = Lock() # For serializing data sent over the network
        self.globalLock = Lock() # For serializing access to global object data
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(None)
        self.shutdown = False

    def handleSocketError(self, error):
        """Call this when a error occurs using our socket.  It forces the
        daemon to close its connection, causing the listen loop to end.  On
        the downloader, this causes the downloader to quit.  On the
        controller side, this causes the controller to restart the downloader.
        """
        print "socket error in daemon, closing my socket"
        self.stream._sock.shutdown(socket.SHUT_RDWR)

    def listenLoop(self):
        while True:
            #print "Top of dl daemon listen loop"
            (size,) = unpack("I", self.stream.read(SIZE_OF_INT))
            comm = cPickle.loads(self.stream.read(size))
            #print "dl daemon got object %s %s" % (str(comm), comm.id)
            # Process commands in their own thread so actions that
            # need to send stuff over the wire don't hang
            # FIXME: We shouldn't spawn a thread for every command!
            if not isinstance(comm, command.ShutDownCommand):
                if comm.orig:
                    eventloop.addIdle(self.processCommand,
                                      "DL Daemon Process Command", args=(comm,))
                else:
                    self.processReturnValue(comm)
            else:
                # Process the shutdown command in the current thread. In the
                # downloader daemon, this waits for all other threads to quit,
                # so processing it in a separate thread would deadlock.
                if comm.orig:
                    self.processCommand(comm)
                    # give the controller some time to get the reply before we
                    # close our socket
                    sleep(0.1)
                self.shutdown = True
                self.globalLock.acquire()
                events = []
                try:
                    for comm in self.waitingCommands.keys():
                        events = events + (self.waitingCommands[comm.id],)
                        del self.waitingCommands[comm.id]
                        self.returnValues[comm.id] = DaemonError("Connection shutdown")
                finally:
                    self.globalLock.release()
                for event in events:
                    event.set()
                break

    def processCommand(self, comm):
        if (self.shutdown):
            return
        comm.setDaemon(self)
        ret = comm.action()
        comm.setReturnValue(ret)
        comm.send(block=False)

    def processReturnValue(self, comm):
        self.globalLock.acquire()
        try:
            if self.waitingCommands.has_key(comm.id):
                event = self.waitingCommands[comm.id]
                del self.waitingCommands[comm.id]
                self.returnValues[comm.id] = comm.getReturnValue()
            else:
                return
        finally:
            self.globalLock.release()
        event.set()

    def waitForReturn(self, comm):
        self.globalLock.acquire()
        try:
            if self.shutdown:
                raise DaemonError ("Connection shutdown")
            if self.waitingCommands.has_key(comm.id):
                event = self.waitingCommands[comm.id]
            elif self.returnValues.has_key(comm.id):
                ret = self.returnValues[comm.id]
                del self.returnValues[comm.id]
                return ret
        finally:
            self.globalLock.release()
        event.wait(30)
        if not event.isSet():
            raise DaemonError("timeout waiting for response to %s" % comm)
        self.globalLock.acquire()
        try:
            ret = self.returnValues[comm.id]
            del self.returnValues[comm.id]
            if isinstance(ret, DaemonError):
                raise ret
            return ret
        finally:
            self.globalLock.release()
            
    def addToWaitingList(self, comm):
        self.globalLock.acquire()
        try:
            self.waitingCommands[comm.id] = Event()
        finally:
            self.globalLock.release()

    def send(self, comm, block):
        if block:
            self.addToWaitingList(comm)
        raw = cPickle.dumps(comm, cPickle.HIGHEST_PROTOCOL)
        self.sendLock.acquire()
        try:
            self.stream.write(pack("I",len(raw)))
            self.stream.write(raw)
            self.stream.flush()
        finally:
            self.sendLock.release()
        if block:
            return self.waitForReturn(comm)


class DownloaderDaemon(Daemon):
    def __init__(self, port):
        # before anything else, write out our PID 
        writePid(os.getpid())
        # connect to the controller and start our listen loop
        Daemon.__init__(self)
        self.socket.connect(('127.0.0.1', port))
        self.stream = self.socket.makefile("r+b")
        print "Downloader Daemon: Connected on port %s" % port
        t = Thread(target = self.downloaderLoop, name = "Downloader Loop")
        t.start()

    def downloaderLoop(self):
        self.listenLoop()
        print "Downloader listen loop completed"

class ControllerDaemon(Daemon):
    def __init__(self):
        Daemon.__init__(self)
        # open a port and start our listen loop
        self.socket.bind( ('127.0.0.1', 0) )
        (myAddr, myPort) = self.socket.getsockname()
        print "Controller Daemon: Listening on %s %s" % (myAddr, myPort)
        self.port = myPort
        self.socket.listen(63)
        self.ready = Event()
        self.shutdownEvent = Event()
        self.hardShutdown = False
        t = Thread(target = self.controllerLoop, name = "Controller Loop")
        t.start()
        self.ready.wait()

    def send(self, comm, block):
        # Don't let traffic through until tho downloader child process is
        # ready
        if (not isinstance(comm, command.InitialConfigCommand)) and comm.orig and not self.ready.isSet():
            print 'DTV: Delaying send of %s %s' % (str(comm), comm.id)
            if block:
                self.ready.wait()
            else:
                raise socket.error("server not ready")
        return Daemon.send(self, comm, block)


    def cleanupAfterError(self):
        """Called when there's an error communicating with the downloader
        daemon.  It tries to reset our state so that we're ready to start a
        new downloader daemon.
        """

        self.ready.clear()
        events = []
        self.globalLock.acquire()
        try:
            for id in self.waitingCommands.keys():
                events.append(self.waitingCommands[id])
                del self.waitingCommands[id]
                self.returnValues[id] = \
                        DaemonError("Downloader connection closed")
        finally:
            self.globalLock.release()
        for e in events:
            e.set()

    def controllerLoop(self):
        try:
            while True:
                self.connectToDownloader()
                try:
                    self.listenLoop()
                    print "Controller listen loop completed"
                    break
                except Exception, e:
                    if self.hardShutdown:
                        break
                    self.cleanupAfterError()
                    import util
                    util.failedExn("While talking to downloader backend")
                    # On socket errors, the downloader dies, but the
                    # controller stays alive and restarts the downloader
                    # by continuing the while loop we achieve this
        finally:
            self.shutdownEvent.set()

    def connectToDownloader(self):
        # launch a new daemon
        launchDownloadDaemon(readPid(), self.port)
        # wait for the daemon to connect to our port
        (conn, address) = self.socket.accept()
        conn.settimeout(None)
        self.stream = conn.makefile("r+b")
        from dl_daemon import remoteconfig
        import config
        data = {}
        for desc in remoteconfig.getConfigItems():
            data[desc.key] = config.get(desc)
        c = command.InitialConfigCommand(self, data)
        c.send(block=False)
        self.ready.set()

    def shutdownDownloaderDaemon(self, timeout=5):
        """Send the downloader daemon the shutdown command.  If it doesn't
        reply before timeout expires, kill it.  (The reply is not sent until
        the downloader daemon has one remaining thread and that thread will
        immediately exit).
        """

        c = command.ShutDownCommand(self)
        c.send(block=False)
        self.shutdownEvent.wait(timeout)
        if not self.shutdownEvent.isSet():
            print "Downloader daemon didn't quit after %d seconds" % timeout
            print "Starting hard shutdown"
            self.hardShutdown = True
            self.stream._sock.shutdown(socket.SHUT_RDWR)
            self.shutdownEvent.wait()
            import app
            delegate = app.controller.getBackendDelegate()
            delegate.killDownloadDaemon(readPid())
