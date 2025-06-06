"""A set of common tools to be used in pilot commands"""

from __future__ import absolute_import, division, print_function

import fcntl
import getopt
import json
import os
import re
import select
import signal
import ssl
import subprocess
import sys
import threading
import warnings
from datetime import datetime
from functools import partial, wraps
from threading import RLock

############################
# python 2 -> 3 "hacks"
try:
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import urlopen
except ImportError:
    from urllib import urlencode

    from urllib2 import HTTPError, URLError, urlopen

try:
    import importlib.util
    from importlib import import_module

    def load_module_from_path(module_name, path_to_module):
        spec = importlib.util.spec_from_file_location(module_name, path_to_module)  # pylint: disable=no-member
        module = importlib.util.module_from_spec(spec)  # pylint: disable=no-member
        spec.loader.exec_module(module)
        return module

except ImportError:

    def import_module(module):
        import imp

        impData = imp.find_module(module)
        return imp.load_module(module, *impData)

    def load_module_from_path(module_name, path_to_module):
        import imp

        fp, pathname, description = imp.find_module(module_name, [path_to_module])
        try:
            return imp.load_module(module_name, fp, pathname, description)
        finally:
            if fp:
                fp.close()


try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO

try:
    basestring  # pylint: disable=used-before-assignment
except NameError:
    basestring = str

try:
    from Pilot.proxyTools import getVO
except ImportError:
    from proxyTools import getVO

try:
    FileNotFoundError  # pylint: disable=used-before-assignment
    # because of https://github.com/PyCQA/pylint/issues/6748
except NameError:
    FileNotFoundError = OSError

try:
    IsADirectoryError  # pylint: disable=used-before-assignment
except NameError:
    IsADirectoryError = IOError

# Timer 2.7 and < 3.3 versions issue where Timer is a function
if sys.version_info.major == 2 or sys.version_info.major == 3 and sys.version_info.minor < 3:
    from threading import _Timer as Timer  # pylint: disable=no-name-in-module
else:
    from threading import Timer

# Utilities functions


def parseVersion(releaseVersion):
    """Convert the releaseVersion into a legacy or PEP-440 style string

    :param str releaseVersion: The software version to use
    """
    VERSION_PATTERN = re.compile(r"^(?:v)?(\d+)[r\.](\d+)(?:[p\.](\d+))?(?:(?:-pre|a)?(\d+))?$")

    match = VERSION_PATTERN.match(releaseVersion)
    # If the regex fails just return the original version
    if not match:
        return releaseVersion
    major, minor, patch, pre = match.groups()
    version = major + "." + minor
    version += "." + (patch or "0")
    if pre:
        version += "a" + pre
    return version


def pythonPathCheck():
    """Checks where python is located

    Raises:
        - An exception if getting the env path raises an error
        - An exception in case there is an error while removing duplicates in the PYTHONPATH
    """
    try:
        os.umask(18)  # 022
        pythonpath = os.getenv("PYTHONPATH", "").split(":")
        print("Directories in PYTHONPATH:", pythonpath)
        for p in pythonpath:
            if p == "":
                continue
            try:
                if os.path.normpath(p) in sys.path:
                    # In case a given directory is twice in PYTHONPATH it has to removed only once
                    sys.path.remove(os.path.normpath(p))
            except Exception as pathRemovalError:
                print(pathRemovalError)
                print("[EXCEPTION-info] Failing path:", p, os.path.normpath(p))
                print("[EXCEPTION-info] sys.path:", sys.path)
                raise pathRemovalError
    except Exception as envError:
        print(envError)
        print("[EXCEPTION-info] sys.executable:", sys.executable)
        print("[EXCEPTION-info] sys.version:", sys.version)
        print("[EXCEPTION-info] os.uname():", os.uname())
        raise envError


def alarmTimeoutHandler(*args):
    raise Exception("Timeout")


def retrieveUrlTimeout(url, fileName, log, timeout=0):
    """
    Retrieve remote url to local file, with timeout wrapper
    """
    urlData = ""
    if timeout:
        signal.signal(signal.SIGALRM, alarmTimeoutHandler)
        # set timeout alarm
        signal.alarm(timeout + 5)
    try:
        remoteFD = urlopen(url)
        expectedBytes = 0
        # Sometimes repositories do not return Content-Length parameter
        try:
            expectedBytes = int(remoteFD.info()["Content-Length"])
        except Exception:
            expectedBytes = 0
        data = remoteFD.read()
        if fileName:
            with open(fileName, "wb") as localFD:
                localFD.write(data)
        else:
            urlData += data
        remoteFD.close()
        if len(data) != expectedBytes and expectedBytes > 0:
            log.error("URL retrieve: expected size does not match the received one")
            return False

        if timeout:
            signal.alarm(0)
        if fileName:
            return True
        return urlData

    except HTTPError as x:
        if x.code == 404:
            log.error("URL retrieve: %s does not exist" % url)
            if timeout:
                signal.alarm(0)
            return False
    except URLError:
        log.error('Timeout after %s seconds on transfer request for "%s"' % (str(timeout), url))
        return False
    except Exception as x:
        if x == "Timeout":
            log.error('Timeout after %s seconds on transfer request for "%s"' % (str(timeout), url))
        if timeout:
            signal.alarm(0)
        raise x


def safe_listdir(directory, timeout=60):
    """This is a "safe" list directory,
    for lazily-loaded File Systems like CVMFS.
    There's by default a 60 seconds timeout.

    .. warning::
        There is no distinction between an empty directory, and a non existent one.
        It will return `[]` in both cases.

    :param str directory: directory to list
    :param int timeout: optional timeout, in seconds. Defaults to 60.
    """

    def listdir(directory):
        try:
            return os.listdir(directory)
        except FileNotFoundError:
            print("%s not found" % directory)
            return []

    contents = []
    t = threading.Thread(target=lambda: contents.extend(listdir(directory)))
    t.daemon = True  # don't delay program's exit
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None  # timeout
    return contents


def getSubmitterInfo(ceName):
    """Get information about the submitter of the pilot.

    Check the environment variables to determine the type of batch system and CE used
    to submit the pilot being used and return this information in a tuple.
    """

    pilotReference = os.environ.get("DIRAC_PILOT_STAMP", "")
    # Batch system taking care of the pilot
    # Might be useful to extract the info to interact with it later on
    batchSystemType = "Unknown"
    batchSystemJobID = "Unknown"
    batchSystemParameters = {
        "BinaryPath": "Unknown",
        "Host": "Unknown",
        "InfoPath": "Unknown",
        "Queue": "Unknown",
    }
    # Flavour of the pilot
    # Inform whether the pilot was sent through SSH+batch system or a CE
    flavour = "DIRAC"

    # # Batch systems

    # Torque
    if "PBS_JOBID" in os.environ:
        batchSystemType = "PBS"
        batchSystemJobID = os.environ["PBS_JOBID"]
        batchSystemParameters["BinaryPath"] = os.environ.get("PBS_O_PATH", "Unknown")
        batchSystemParameters["Queue"] = os.environ.get("PBS_O_QUEUE", "Unknown")

        flavour = "SSH%s" % batchSystemType
        pilotReference = "sshpbs://" + ceName + "/" + batchSystemJobID.split(".")[0]

    # OAR
    if "OAR_JOBID" in os.environ:
        batchSystemType = "OAR"
        batchSystemJobID = os.environ["OAR_JOBID"]

        flavour = "SSH%s" % batchSystemType
        pilotReference = "sshoar://" + ceName + "/" + batchSystemJobID

    # Grid Engine
    if "SGE_TASK_ID" in os.environ:
        batchSystemType = "SGE"
        batchSystemJobID = os.environ["JOB_ID"]
        batchSystemParameters["BinaryPath"] = os.environ.get("SGE_BINARY_PATH", "Unknown")
        batchSystemParameters["Queue"] = os.environ.get("QUEUE", "Unknown")

        flavour = "SSH%s" % batchSystemType
        pilotReference = "sshge://" + ceName + "/" + batchSystemJobID

    # LSF
    if "LSB_BATCH_JID" in os.environ:
        batchSystemType = "LSF"
        batchSystemJobID = os.environ["LSB_BATCH_JID"]
        batchSystemParameters["BinaryPath"] = os.environ.get("LSF_BINDIR", "Unknown")
        batchSystemParameters["Host"] = os.environ.get("LSB_HOSTS", "Unknown")
        batchSystemParameters["InfoPath"] = os.environ.get("LSF_ENVDIR", "Unknown")
        batchSystemParameters["Queue"] = os.environ.get("LSB_QUEUE", "Unknown")

        flavour = "SSH%s" % batchSystemType
        pilotReference = "sshlsf://" + ceName + "/" + batchSystemJobID

    #  SLURM
    if "SLURM_JOBID" in os.environ:
        batchSystemType = "SLURM"
        batchSystemJobID = os.environ["SLURM_JOBID"]

        flavour = "SSH%s" % batchSystemType
        pilotReference = "sshslurm://" + ceName + "/" + batchSystemJobID

    # HTCondor
    if "_CONDOR_JOB_AD" in os.environ:
        batchSystemType = "HTCondor"
        batchSystemJobID = None  # Not available in the environment
        batchSystemParameters["InfoPath"] = os.environ["_CONDOR_JOB_AD"]

        flavour = "SSH%s" % batchSystemType
        pilotReference = "sshcondor://" + ceName + "/" + os.environ.get("CONDOR_JOBID", pilotReference)

    # # Local/SSH

    # Local submission to the host
    if "LOCAL_JOBID" in os.environ:
        flavour = "Local"
        pilotReference = "local://" + ceName + "/" + os.environ["LOCAL_JOBID"]

    # Direct SSH tunnel submission
    if "SSHCE_JOBID" in os.environ:
        flavour = "SSH"
        pilotReference = "ssh://" + ceName + "/" + os.environ["SSHCE_JOBID"]

    # Batch host SSH tunnel submission (SSHBatch CE)
    if "SSHBATCH_JOBID" in os.environ and "SSH_NODE_HOST" in os.environ:
        flavour = "SSHBATCH"
        pilotReference = (
            "sshbatchhost://" + ceName + "/" + os.environ["SSH_NODE_HOST"] + "/" + os.environ["SSHBATCH_JOBID"]
        )

    # # CEs

    # HTCondor
    if "HTCONDOR_JOBID" in os.environ:
        flavour = "HTCondorCE"
        pilotReference = "htcondorce://" + ceName + "/" + os.environ["HTCONDOR_JOBID"]

    # ARC
    if "GRID_GLOBAL_JOBURL" in os.environ:
        flavour = "ARC"
        pilotReference = os.environ["GRID_GLOBAL_JOBURL"]

    # Cloud case
    if "PILOT_UUID" in os.environ:
        flavour = "CLOUD"
        pilotReference = os.environ["PILOT_UUID"]

    return (
        flavour,
        pilotReference,
        {"Type": batchSystemType, "JobID": batchSystemJobID, "Parameters": batchSystemParameters},
    )


def getFlavour(ceName):
    """Old method to get the flavour of the pilot. Deprecated.

    Please use getSubmitterInfo instead.
    """
    warnings.warn(
        "getFlavour() is deprecated. Please use getSubmitterInfo() instead.", category=DeprecationWarning, stacklevel=2
    )
    flavour, pilotReference, _ = getSubmitterInfo(ceName)
    return flavour, pilotReference


class ObjectLoader(object):
    """Simplified class for loading objects from a DIRAC installation.

    Example:

    ```py
    ol = ObjectLoader()
    object, modulePath = ol.loadObject( 'pilot', 'LaunchAgent' )
    ```
    """

    def __init__(self, baseModules, log):
        """init"""
        self.__rootModules = baseModules
        self.log = log

    def loadModule(self, modName, hideExceptions=False):
        """Auto search which root module has to be used"""
        for rootModule in self.__rootModules:
            impName = modName
            if rootModule:
                impName = "%s.%s" % (rootModule, impName)
            self.log.debug("Trying to load %s" % impName)
            module, parentPath = self.__recurseImport(impName, hideExceptions=hideExceptions)
            # Error. Something cannot be imported. Return error
            if module is None:
                return None, None
            # Huge success!
            return module, parentPath
            # Nothing found, continue
        # Return nothing found
        return None, None

    def __recurseImport(self, modName, parentModule=None, hideExceptions=False):
        """Internal function to load modules"""
        if isinstance(modName, basestring):
            modName = modName.split(".")
        try:
            if parentModule:
                impModule = load_module_from_path(modName[0], parentModule.__path__)
            else:
                impModule = import_module(modName[0])
        except ImportError as excp:
            if str(excp).find("No module named %s" % modName[0]) == 0:
                return None, None
            errMsg = "Can't load %s in %s" % (".".join(modName), parentModule.__path__[0])
            if not hideExceptions:
                self.log.exception(errMsg)
            return None, None
        if len(modName) == 1:
            return impModule, parentModule.__path__[0]
        return self.__recurseImport(modName[1:], impModule, hideExceptions=hideExceptions)

    def loadObject(self, package, moduleName, command):
        """Load an object from inside a module"""
        loadModuleName = "%s.%s" % (package, moduleName)
        module, parentPath = self.loadModule(loadModuleName)
        if module is None:
            return None, None
        try:
            commandObj = getattr(module, command)
            return commandObj, os.path.join(parentPath, moduleName)
        except AttributeError as e:
            self.log.error("Exception: %s" % str(e))
            return None, None


def getCommand(params, commandName):
    """Get an instantiated command object for execution.
    Commands are looked in the following modules in the order:

    1. `<CommandExtension>PilotCommands`
    2. `PilotCommands`
    """
    extensions = params.commandExtensions
    modules = [m + "Commands" for m in extensions + ["pilot"]]
    commandObject = None

    # Look for commands in the modules in the current directory first
    for module in modules:
        try:
            commandModule = import_module(module)
            commandObject = getattr(commandModule, commandName)
        except Exception:
            pass
        if commandObject:
            return commandObject(params), module

    # No command could be instantiated
    return None, None


class Logger(object):
    """Basic logger object, for use inside the pilot. Just using print."""

    def __init__(self, name="Pilot", debugFlag=False, pilotOutput="pilot.out"):
        self.debugFlag = debugFlag
        self.name = name
        self.out = pilotOutput
        self._headerTemplate = "{datestamp} {{level}} [{name}] {{message}}"

    @property
    def messageTemplate(self):
        """
        Message template in ISO-8601 format.

        :return: template string
        :rtype: str
        """
        return self._headerTemplate.format(
            datestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            name=self.name,
        )

    def __outputMessage(self, msg, level, header):
        if self.out:
            with open(self.out, "a") as outputFile:
                for _line in str(msg).split("\n"):
                    if header:
                        outLine = self.messageTemplate.format(level=level, message=_line)
                        print(outLine)
                        if self.out:
                            outputFile.write(outLine + "\n")
                    else:
                        print(_line)
                        outputFile.write(_line + "\n")

        sys.stdout.flush()

    def setDebug(self):
        self.debugFlag = True

    def debug(self, msg, header=True):
        if self.debugFlag:
            self.__outputMessage(msg, "DEBUG", header)

    def error(self, msg, header=True):
        self.__outputMessage(msg, "ERROR", header)

    def warn(self, msg, header=True):
        self.__outputMessage(msg, "WARN", header)

    def info(self, msg, header=True):
        self.__outputMessage(msg, "INFO", header)


class RemoteLogger(Logger):
    """
    The remote logger object, for use inside the pilot. It prints messages,
    but can be also used to send messages to an external service.
    """

    def __init__(
        self,
        url,
        name="Pilot",
        debugFlag=False,
        pilotOutput="pilot.out",
        isPilotLoggerOn=True,
        pilotUUID="unknown",
        flushInterval=10,
        bufsize=1000,
        wnVO="unknown",
    ):
        """
        c'tor
        If flag PilotLoggerOn is not set, the logger will behave just like
        the original Logger object, that means it will just print logs locally on the screen
        """
        super(RemoteLogger, self).__init__(name, debugFlag, pilotOutput)
        self.url = url
        self.pilotUUID = pilotUUID
        self.wnVO = wnVO
        self.isPilotLoggerOn = isPilotLoggerOn
        sendToURL = partial(sendMessage, url, pilotUUID, wnVO, "sendMessage")
        self.buffer = FixedSizeBuffer(sendToURL, bufsize=bufsize, autoflush=flushInterval)

    def debug(self, msg, header=True, _sendPilotLog=False):
        # TODO: Send pilot log remotely?
        super(RemoteLogger, self).debug(msg, header)
        if (
            self.isPilotLoggerOn and self.debugFlag
        ):  # the -d flag activates this debug flag in CommandBase via PilotParams
            self.sendMessage(self.messageTemplate.format(level="DEBUG", message=msg))

    def error(self, msg, header=True, _sendPilotLog=False):
        # TODO: Send pilot log remotely?
        super(RemoteLogger, self).error(msg, header)
        if self.isPilotLoggerOn:
            self.sendMessage(self.messageTemplate.format(level="ERROR", message=msg))

    def warn(self, msg, header=True, _sendPilotLog=False):
        # TODO: Send pilot log remotely?
        super(RemoteLogger, self).warn(msg, header)
        if self.isPilotLoggerOn:
            self.sendMessage(self.messageTemplate.format(level="WARNING", message=msg))

    def info(self, msg, header=True, _sendPilotLog=False):
        # TODO: Send pilot log remotely?
        super(RemoteLogger, self).info(msg, header)
        if self.isPilotLoggerOn:
            self.sendMessage(self.messageTemplate.format(level="INFO", message=msg))

    def sendMessage(self, msg):
        """
        Buffered message sender.

        :param msg: message to send
        :type msg: str
        :return: None
        :rtype: None
        """
        try:
            self.buffer.write(msg + "\n")
        except Exception as err:
            super(RemoteLogger, self).error("Message not sent")
            super(RemoteLogger, self).error(str(err))


def synchronized(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._rlock:
            return func(self, *args, **kwargs)

    return wrapper


class RepeatingTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


class FixedSizeBuffer(object):
    """
    A buffer with a (preferred) fixed number of lines.
    Once it's full, a message is sent to a remote server and the buffer is renewed.
    """

    def __init__(self, senderFunc, bufsize=1000, autoflush=10):
        """
        Constructor.

        :param senderFunc: a function used to send a message
        :type senderFunc: func
        :param bufsize: size of the buffer (in lines)
        :type bufsize: int
        :param autoflush: buffer flush period in seconds
        :type autoflush: int
        """

        self._rlock = RLock()
        if autoflush > 0:
            self._timer = RepeatingTimer(autoflush, self.flush)
            self._timer.start()
        else:
            self._timer = None
        self.output = StringIO()
        self.bufsize = bufsize
        self._nlines = 0
        self.senderFunc = senderFunc

    @synchronized
    def write(self, text):
        """
        Write text to a string buffer. Newline characters are counted and number of lines in the buffer
        is increased accordingly.

        :param text: text string to write
        :type text: str
        :return: None
        :rtype: None
        """
        # reopen the buffer in a case we had to flush a partially filled buffer
        if self.output.closed:
            self.output = StringIO()
        self.output.write(text)
        self._nlines += max(1, text.count("\n"))
        self.sendFullBuffer()

    @synchronized
    def getValue(self):
        content = self.output.getvalue()
        return content

    @synchronized
    def sendFullBuffer(self):
        """
        Get the buffer content, send a message, close the current buffer and re-create a new one for subsequent writes.

        """

        if self._nlines >= self.bufsize:
            self.flush()
            self.output = StringIO()

    @synchronized
    def flush(self):
        """
        Flush the buffer and send log records to a remote server. The buffer is closed as well.

        :return: None
        :rtype:  None
        """
        if not self.output.closed and self._nlines > 0:
            self.output.flush()
            buf = self.getValue()
            self.senderFunc(buf)
            self._nlines = 0
            self.output.close()

    def cancelTimer(self):
        """
        Cancel the repeating timer if it exists.

        :return: None
        :rtype: None
        """
        if self._timer is not None:
            self._timer.cancel()


def sendMessage(url, pilotUUID, wnVO, method, rawMessage):
    """
    Invoke a remote method on a Tornado server and pass a JSON message to it.

    :param str url: Server URL
    :param str pilotUUID: pilot unique ID
    :param str wnVO: VO name, relevant only if not contained in a proxy
    :param str method: a method to be invoked
    :param str rawMessage: a message to be sent, in JSON format
    :return: None.
    """
    caPath = os.getenv("X509_CERT_DIR")
    cert = os.getenv("X509_USER_PROXY")

    context = ssl.create_default_context()
    context.load_verify_locations(capath=caPath)

    message = json.dumps((json.dumps(rawMessage), pilotUUID, wnVO))

    try:
        context.load_cert_chain(cert)  # this is a proxy
        raw_data = {"method": method, "args": message}
    except IsADirectoryError:  # assuming it'a dir containing cert and key
        context.load_cert_chain(os.path.join(cert, "hostcert.pem"), os.path.join(cert, "hostkey.pem"))
        raw_data = {"method": method, "args": message, "extraCredentials": '"hosts"'}

    if sys.version_info.major == 3:
        data = urlencode(raw_data).encode("utf-8")  # encode to bytes ! for python3
    else:
        # Python2
        data = urlencode(raw_data)

    res = urlopen(url, data, context=context)
    res.close()


class CommandBase(object):
    """CommandBase is the base class for every command in the pilot commands toolbox"""

    def __init__(self, pilotParams):
        """
        Defines the classic pilot logger and the pilot parameters.
        Debug level of the Logger is controlled by the -d flag in pilotParams.

        :param pilotParams: a dictionary of pilot parameters.
        :type pilotParams: dict
        :param dummy:
        """

        self.pp = pilotParams
        self.debugFlag = pilotParams.debugFlag
        loggerURL = pilotParams.loggerURL
        # URL present and the flag is set:
        isPilotLoggerOn = pilotParams.pilotLogging and (loggerURL is not None)
        interval = pilotParams.loggerTimerInterval
        bufsize = pilotParams.loggerBufsize

        if not isPilotLoggerOn:
            self.log = Logger(self.__class__.__name__, debugFlag=self.debugFlag)
        else:
            # remote logger
            self.log = RemoteLogger(
                loggerURL,
                self.__class__.__name__,
                pilotUUID=pilotParams.pilotUUID,
                debugFlag=self.debugFlag,
                flushInterval=interval,
                bufsize=bufsize,
                wnVO=pilotParams.wnVO,
            )

        self.log.isPilotLoggerOn = isPilotLoggerOn
        if self.debugFlag:
            self.log.setDebug()

        self.log.debug("Initialized command %s" % self.__class__.__name__)
        self.log.debug("pilotParams option list: %s" % self.pp.optList)

    def executeAndGetOutput(self, cmd, environDict=None):
        """Execute a command on the worker node and get the output"""

        self.log.info("Executing command %s" % cmd)
        _p = subprocess.Popen(
            cmd, shell=True, env=environDict, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=False
        )

        # Use non-blocking I/O on the process pipes
        for fd in [_p.stdout.fileno(), _p.stderr.fileno()]:
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        outData = ""
        while True:
            readfd, _, _ = select.select([_p.stdout, _p.stderr], [], [])
            dataWasRead = False
            for stream in readfd:
                outChunk = stream.read().decode("ascii", "replace")
                if not outChunk:
                    continue
                dataWasRead = True
                if sys.version_info.major == 2:
                    # Ensure outChunk is unicode in Python 2
                    if isinstance(outChunk, str):
                        outChunk = outChunk.decode("utf-8")
                    # Strip unicode replacement characters
                    # Ensure correct type conversion in Python 2
                    outChunk = str(outChunk.replace(u"\ufffd", ""))
                    # Avoid potential str() issues in Py2
                    outChunk = unicode(outChunk)  # pylint: disable=undefined-variable
                else:
                    outChunk = str(outChunk.replace("\ufffd", ""))  # Python 3: Ensure it's a string

                if stream == _p.stderr:
                    sys.stderr.write(outChunk)
                    sys.stderr.flush()
                else:
                    sys.stdout.write(outChunk)
                    sys.stdout.flush()
                    if hasattr(self.log, "buffer") and self.log.isPilotLoggerOn:
                        self.log.buffer.write(outChunk)
                    outData += outChunk
            # If no data was read on any of the pipes then the process has finished
            if not dataWasRead:
                break

        # Ensure output ends on a newline
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.stderr.write("\n")
        sys.stderr.flush()

        # return code
        returnCode = _p.wait()
        self.log.debug("Return code of %s: %d" % (cmd, returnCode))

        return (returnCode, outData)

    def exitWithError(self, errorCode):
        """Wrapper around sys.exit()"""
        self.log.info("Content of pilot.cfg")
        with open("pilot.cfg") as f:
            print(f.read())

        self.log.info("List of child processes of current PID:")
        retCode, _outData = self.executeAndGetOutput(
            "ps --forest -o pid,%%cpu,%%mem,tty,stat,time,cmd -g %d" % os.getpid()
        )
        if retCode:
            self.log.error("Failed to issue ps [ERROR %d] " % retCode)
        sys.exit(errorCode)

    def forkAndExecute(self, cmd, logFile, environDict=None):
        """Fork and execute a command on the worker node"""

        self.log.info("Fork and execute command %s" % cmd)
        pid = os.fork()

        if pid != 0:
            # Still in the parent, return the subprocess ID
            return pid

        # The subprocess stdout/stderr will be written to logFile
        with open(logFile, "a+", 0) as fpLogFile:
            try:
                _p = subprocess.Popen(
                    "%s" % cmd, shell=True, env=environDict, close_fds=False, stdout=fpLogFile, stderr=fpLogFile
                )

                # return code
                returnCode = _p.wait()
                self.log.debug("Return code of %s: %d" % (cmd, returnCode))
            except BaseException:
                returnCode = 99

        sys.exit(returnCode)

    @property
    def releaseVersion(self):
        parsedVersion = parseVersion(self.pp.releaseVersion)
        # strip what is not strictly the version number (e.g. if it is DIRAC[pilot]==7.3.4])
        return parsedVersion.split("==")[1] if "==" in parsedVersion else parsedVersion


class PilotParams(object):
    """Class that holds the structure with all the parameters to be used across all the commands"""

    def __init__(self):
        """c'tor

        param names and defaults are defined here
        """
        self.log = Logger(self.__class__.__name__, debugFlag=True)
        self.rootPath = os.getcwd()
        self.pilotRootPath = os.getcwd()
        self.workingDir = os.getcwd()

        self.optList = {}
        self.keepPythonPath = False
        self.debugFlag = False
        self.local = False
        self.pilotJSON = None
        self.commandExtensions = []
        self.commands = [
            "CheckWorkerNode",
            "InstallDIRAC",
            "ConfigureBasics",
            "RegisterPilot",
            "CheckCECapabilities",
            "CheckWNCapabilities",
            "ConfigureSite",
            "ConfigureArchitecture",
            "ConfigureCPURequirements",
            "LaunchAgent",
        ]
        self.commandOptions = {}
        self.extensions = []
        self.tags = []
        self.reqtags = []
        self.site = ""
        self.setup = ""
        self.configServer = ""
        self.ceName = ""
        self.ceType = ""
        self.queueName = ""
        self.gridCEType = ""
        # maxNumberOfProcessors: the number of
        # processors allocated to the pilot which the pilot can allocate to one payload
        # used to set payloadProcessors unless other limits are reached (like the number of processors on the WN)
        self.maxNumberOfProcessors = 0
        self.minDiskSpace = 2560  # MB
        self.userGroup = ""
        self.userDN = ""
        self.maxCycles = 10
        self.pollingTime = 20
        self.stopOnApplicationFailure = True
        self.stopAfterFailedMatches = 10
        self.flavour = "DIRAC"
        self.batchSystemInfo = {}
        self.pilotReference = ""
        self.releaseVersion = ""
        self.releaseProject = ""
        self.gateway = ""
        self.useServerCertificate = False
        self.pilotScriptName = ""
        self.genericOption = ""
        self.wnVO = ""  # for binding the resource (WN) to a specific VO
        # Some commands can define environment necessary to execute subsequent commands
        self.installEnv = os.environ
        # If DIRAC is preinstalled this file will receive the updates of the local configuration
        self.localConfigFile = "pilot.cfg"
        self.preinstalledEnv = ""
        self.preinstalledEnvPrefix = ""
        self.executeCmd = False
        self.configureScript = "dirac-configure"
        self.architectureScript = "dirac-platform"
        self.certsLocation = "%s/etc/grid-security" % self.workingDir
        self.pilotCFGFile = "pilot.json"
        self.pilotLogging = False
        self.loggerURL = None
        self.loggerTimerInterval = 0
        self.loggerBufsize = 1000
        self.pilotUUID = "unknown"
        self.modules = ""
        self.userEnvVariables = ""
        self.pipInstallOptions = ""
        self.CVMFS_locations = [
            "/cvmfs/grid.cern.ch",
            "/cvmfs/dirac.egi.eu",
        ]

        # Parameters that can be determined at runtime only
        self.queueParameters = {}  # from CE description
        self.jobCPUReq = 900  # HS06s, here just a random value

        # Set number of allocatable processors from MJF if available
        try:
            self.pilotProcessors = int(urlopen(os.path.join(os.environ["JOBFEATURES"], "allocated_cpu")).read())
        except Exception:
            self.pilotProcessors = 1

        # Pilot command options
        self.cmdOpts = (
            ("", "requiredTag=", "extra required tags for resource description"),
            ("a:", "gridCEType=", "Grid CE Type (CREAM etc)"),
            ("c", "cert", "Use server certificate instead of proxy"),
            ("d", "debug", "Set debug flag"),
            ("e:", "extraPackages=", "Extra packages to install (comma separated)"),
            ("g:", "loggerURL=", "Remote Logger service URL"),
            ("h", "help", "Show this help"),
            ("k", "keepPP", "Do not clear PYTHONPATH on start"),
            ("l:", "project=", "Project to install"),
            ("n:", "name=", "Set <Site> as Site Name"),
            ("o:", "option=", "Option=value to add"),
            ("m:", "maxNumberOfProcessors=", "specify a max number of processors to use by the payload inside a pilot"),
            ("", "modules=", "for installing non-released code"),
            (
                "",
                "userEnvVariables=",
                'User-requested environment variables (comma-separated, name and value separated by ":::")',
            ),
            ("", "pipInstallOptions=", "Options to pip install"),
            ("r:", "release=", "DIRAC release to install"),
            ("s:", "section=", "Set base section for relative parsed options"),
            ("t:", "tag=", "extra tags for resource description"),
            ("u:", "url=", "Use <url> to download tarballs"),
            ("x:", "execute=", "Execute instead of JobAgent"),
            ("y:", "CEType=", "CE Type (normally InProcess)"),
            ("z", "pilotLogging", "Activate pilot logging system"),
            ("C:", "configurationServer=", "Configuration servers to use"),
            ("D:", "disk=", "Require at least <space> MB available"),
            ("E:", "commandExtensions=", "Python modules with extra commands"),
            ("F:", "pilotCFGFile=", "Specify pilot CFG file"),
            ("G:", "Group=", "DIRAC Group to use"),
            ("K:", "certLocation=", "Specify server certificate location"),
            ("M:", "MaxCycles=", "Maximum Number of JobAgent cycles to run"),
            ("", "PollingTime=", "JobAgent execution frequency"),
            ("", "StopOnApplicationFailure=", "Stop Job Agent when encounter an application failure"),
            ("", "StopAfterFailedMatches=", "Stop Job Agent after N failed matches"),
            ("N:", "Name=", "CE Name"),
            ("O:", "OwnerDN=", "Pilot OwnerDN (for private pilots)"),
            ("", "wnVO=", "Bind the resource (WN) to a VO"),
            ("P:", "pilotProcessors=", "Number of processors allocated to this pilot"),
            ("Q:", "Queue=", "Queue name"),
            ("R:", "reference=", "Use this pilot reference"),
            ("S:", "setup=", "DIRAC Setup to use"),
            ("T:", "CPUTime=", "Requested CPU Time"),
            ("W:", "gateway=", "Configure <gateway> as DIRAC Gateway during installation"),
            ("X:", "commands=", "Pilot commands to execute"),
            ("Z:", "commandOptions=", "Options parsed by command modules"),
            ("", "pilotUUID=", "pilot UUID"),
            ("", "preinstalledEnv=", "preinstalled pilot environment script location"),
            ("", "preinstalledEnvPrefix=", "preinstalled pilot environment area prefix"),
            ("", "architectureScript=", "architecture script to use"),
            ("", "CVMFS_locations=", "comma-separated list of CVMS locations"),
        )

        # Possibly get Setup and JSON URL/filename from command line
        self.__initCommandLine1()

        # Get main options from the JSON file. Load JSON first to determine the format used.
        self.__loadJSON()
        if "Setups" in self.pilotJSON:
            self.__initJSON()
        else:
            self.__initJSON2()

        # Command line can override options from JSON
        self.__initCommandLine2()

        self.__checkSecurityDir("X509_CERT_DIR", "certificates")
        self.__checkSecurityDir("X509_VOMS_DIR", "vomsdir")
        self.__checkSecurityDir("X509_VOMSES", "vomses")
        # This is needed for the integration tests
        self.installEnv["DIRAC_VOMSES"] = self.installEnv["X509_VOMSES"]
        os.environ["DIRAC_VOMSES"] = os.environ["X509_VOMSES"]

        if self.useServerCertificate:
            self.installEnv["X509_USER_PROXY"] = self.certsLocation
            os.environ["X509_USER_PROXY"] = self.certsLocation

    def __setSecurityDir(self, envName, dirLocation):
        """Set the environment variable of the `envName`, and add it also to the Pilot Parameters

        Args:
            envName (str): The environment to set (ex : `X509_USER_PROXY`)
            dirLocation (str): The path that corresponds to the environment variable
        """
        self.log.debug("Setting %s=%s" % (envName, dirLocation))
        self.installEnv[envName] = dirLocation
        os.environ[envName] = dirLocation

    def __checkSecurityDir(self, envName, dirName):
        """For a given environment variable (that is not necessarily set in the OS), checks if it exists *and* is not empty.

        .. example::
            ```
            self.__checkSecurityDir("X509_VOMSES", "vomses")
            ```
            It will check if `X509_VOMSES` is set, if not, check if one of the CVMFS_locations with "vomses" is a valid candidate.
            If let's say `/cvmfs/dirac.egi.eu/etc/grid-security/vomses` exists, *and* is not empty, sets the OS environment variable `X509_VOMSES` to `/cvmfs/dirac.egi.eu/etc/grid-security/vomses`.


        .. warning::
            If none of the candidates work, it will stop the program.

        Args:
            envName (str): The environment name to try
            dirName (str): The target folder
        """

        # Else, try to find it
        for candidate in self.CVMFS_locations:
            candidateDir = os.path.join(candidate, "etc/grid-security", dirName)
            self.log.debug("Candidate directory for %s is %s" % (envName, candidateDir))

            # Checks if the directory exists *and* isn't empty
            if safe_listdir(candidateDir):
                self.log.debug("Setting %s=%s" % (envName, candidateDir))
                # Set the environment variables to the candidate
                self.__setSecurityDir(envName, candidateDir)
                break
            self.log.debug("%s not found or not a directory" % candidateDir)

            # Check first if the environment variable is set
            # If so, just return
        if envName in os.environ and safe_listdir(os.environ[envName]):
            self.log.debug(
                "%s is set in the host environment as %s, aligning installEnv to it" % (envName, os.environ[envName])
            )
        else:
            # None of the candidates exists, stop the program.
            self.log.error("Could not find/set %s" % envName)
            sys.exit(1)

    def __initCommandLine1(self):
        """Parses and interpret options on the command line: first pass (essential things)"""

        self.optList, __args__ = getopt.getopt(
            sys.argv[1:], "".join([opt[0] for opt in self.cmdOpts]), [opt[1] for opt in self.cmdOpts]
        )
        self.log.debug("Options list: %s" % self.optList)
        for o, v in self.optList:
            if o == "-N" or o == "--Name":
                self.ceName = v
            if o == "-Q" or o == "--Queue":
                self.queueName = v
            elif o == "-a" or o == "--gridCEType":
                self.gridCEType = v
            elif o == "-d" or o == "--debug":
                self.debugFlag = True
            elif o in ("-S", "--setup"):
                self.setup = v
            elif o == "-F" or o == "--pilotCFGFile":
                self.pilotCFGFile = v
            elif o == "--wnVO":
                self.wnVO = v

    def __initCommandLine2(self):
        """
        Parses and interpret options on the command line: second pass
        (overriding discovered parameters, for tests/debug)
        """

        self.optList, __args__ = getopt.getopt(
            sys.argv[1:], "".join([opt[0] for opt in self.cmdOpts]), [opt[1] for opt in self.cmdOpts]
        )
        for o, v in self.optList:
            if o == "-E" or o == "--commandExtensions":
                self.commandExtensions = v.split(",")
            elif o == "-X" or o == "--commands":
                self.commands = v.split(",")
            elif o == "-Z" or o == "--commandOptions":
                for i in v.split(","):
                    self.commandOptions[i.split("=", 1)[0].strip()] = i.split("=", 1)[1].strip()
            elif o == "-e" or o == "--extraPackages":
                self.extensions = v.split(",")
            elif o == "-n" or o == "--name":
                self.site = v
            elif o == "-y" or o == "--CEType":
                self.ceType = v
            elif o == "-k" or o == "--keepPP":
                self.keepPythonPath = True
            elif o in ("-C", "--configurationServer"):
                self.configServer = v
            elif o in ("-G", "--Group"):
                self.userGroup = v
            elif o in ("-x", "--execute"):
                self.executeCmd = v
            elif o in ("-O", "--OwnerDN"):
                self.userDN = v
            elif o == "-m" or o == "--maxNumberOfProcessors":
                self.maxNumberOfProcessors = int(v)
            elif o == "-D" or o == "--disk":
                try:
                    self.minDiskSpace = int(v)
                except ValueError:
                    pass
            elif o == "-r" or o == "--release":
                self.releaseVersion = v.split(",", 1)[0]
            elif o in ("-l", "--project"):
                self.releaseProject = v
            elif o in ("-W", "--gateway"):
                self.gateway = v
            elif o == "-c" or o == "--cert":
                self.useServerCertificate = True
            elif o == "-C" or o == "--certLocation":
                self.certsLocation = v
            elif o == "-M" or o == "--MaxCycles":
                try:
                    self.maxCycles = int(v)
                except ValueError:
                    pass
            elif o == "--PollingTime":
                try:
                    self.pollingTime = int(v)
                except ValueError:
                    pass
            elif o == "--StopOnApplicationFailure":
                self.stopOnApplicationFailure = v
            elif o == "--StopAfterFailedMatches":
                try:
                    self.stopAfterFailedMatches = int(v)
                except ValueError:
                    pass
            elif o in ("-T", "--CPUTime"):
                self.jobCPUReq = v
            elif o == "-P" or o == "--pilotProcessors":
                try:
                    self.pilotProcessors = int(v)
                except BaseException:
                    pass
            elif o == "-z" or o == "--pilotLogging":
                self.pilotLogging = True
            elif o == "-g" or o == "--loggerURL":
                self.loggerURL = v
            elif o == "--pilotUUID":
                self.pilotUUID = v
            elif o in ("-o", "--option"):
                self.genericOption = v
            elif o in ("-t", "--tag"):
                self.tags.append(v)
            elif o == "--requiredTag":
                self.reqtags.append(v)
            elif o == "--modules":
                self.modules = v
            elif o == "--userEnvVariables":
                self.userEnvVariables = v
            elif o == "--pipInstallOptions":
                self.pipInstallOptions = v
            elif o == "--preinstalledEnv":
                self.preinstalledEnv = v
            elif o == "--preinstalledEnvPrefix":
                self.preinstalledEnvPrefix = v
            elif o == "--architectureScript":
                self.architectureScript = v
            elif o == "--CVMFS_locations":
                self.CVMFS_locations = v.split(",")

    def __loadJSON(self):
        """
        Load JSON file and return a dict content.

        :return: None
        """

        self.log.debug("JSON file loaded: %s" % self.pilotCFGFile)
        with open(self.pilotCFGFile, "r") as fp:
            # We save the parsed JSON in case pilot commands need it
            # to read their own options
            self.pilotJSON = json.load(fp)

    def __initJSON2(self):
        """
        Retrieve pilot parameters from the content of JSON dict using a new format, which closer follows the
        CS Operations section. The CE JSON section remains the same. The first difference is present in Commands,
        followed by a new VO-specific sections.

        :return: None
        """

        self.__ceType()
        # Commands first. In the new format they can be either in Defaults/Pilot
        # section or in a VO section (voname/self.setup/Pilot). They are published as a list in a dict
        # keyed by a CE type.
        pilotOptions = self.getPilotOptionsDict()
        self.log.debug("PilotOptionsDict %s " % pilotOptions)
        # remote logging (the default value is self.pilotLogging, a bool)
        pilotLogging = pilotOptions.get("RemoteLogging")
        if pilotLogging is not None:
            self.pilotLogging = pilotLogging.upper() == "TRUE"
        self.loggerURL = pilotOptions.get("RemoteLoggerURL")
        # logger buffer flush interval in seconds.
        self.loggerTimerInterval = int(pilotOptions.get("RemoteLoggerTimerInterval", self.loggerTimerInterval))
        # logger buffer size in lines:
        self.loggerBufsize = max(1, int(pilotOptions.get("RemoteLoggerBufsize", self.loggerBufsize)))
        # logger CE white list
        loggerCEsWhiteList = pilotOptions.get("RemoteLoggerCEsWhiteList")
        # restrict remote logging to whitelisted CEs ([] or None => no restriction)
        self.log.debug("JSON: Remote logging CE white list: %s" % loggerCEsWhiteList)
        if loggerCEsWhiteList is not None:
            if not isinstance(loggerCEsWhiteList, list):
                loggerCEsWhiteList = [elem.strip() for elem in loggerCEsWhiteList.split(",")]
            if self.ceName not in loggerCEsWhiteList:
                self.pilotLogging = False
                self.log.debug("JSON: Remote logging disabled for this CE: %s" % self.ceName)
        pilotLogLevel = pilotOptions.get("PilotLogLevel", "INFO")
        if pilotLogLevel.lower() == "debug":
            self.debugFlag = True
        self.log.debug("JSON: Remote logging: %s" % self.pilotLogging)
        self.log.debug("JSON: Remote logging URL: %s" % self.loggerURL)
        self.log.debug("JSON: Remote logging buffer flush interval in sec.(0: disabled): %s" % self.loggerTimerInterval)
        self.log.debug("JSON: Remote/local logging debug flag: %s" % self.debugFlag)
        self.log.debug("JSON: Remote logging buffer size (lines): %s" % self.loggerBufsize)

        # CE type if present, then Defaults, otherwise as defined in the code:
        if "Commands" in pilotOptions:
            for key in [self.gridCEType, "Defaults"]:
                commands = pilotOptions["Commands"].get(key)
                if commands is not None:
                    if isinstance(commands, list):
                        self.commands = commands
                    else:
                        # TODO: This is a workaround until the pilot JSON syncroniser is fixed
                        self.commands = [elem.strip() for elem in commands.split(",")]
                    self.log.debug("Selecting commands from JSON for Grid CE type %s" % key)
                    break
        else:
            key = "CodeDefaults"

        self.log.debug("Commands[%s]: %s" % (key, self.commands))

        # Command extensions for the commands above:
        commandExtOptions = pilotOptions.get("CommandExtensions")
        if commandExtOptions:
            self.commandExtensions = [elem.strip() for elem in commandExtOptions.split(",")]
        # Configuration server (the synchroniser looks into gConfig.getServersList(), as before
        # the generic one (a list):
        self.configServer = ",".join([str(pv).strip() for pv in self.pilotJSON["ConfigurationServers"]])

        # version(a comma separated values in a string). We take the first one. (the default value defined in the code)
        dVersion = pilotOptions.get("Version", self.releaseVersion)
        if dVersion:
            dVersion = [dv.strip() for dv in dVersion.split(",", 1)]
            self.releaseVersion = str(dVersion[0])
        else:
            self.log.warn("Could not find a version in the JSON file configuration")

        self.log.debug("Version: %s -> (release) %s" % (str(dVersion), self.releaseVersion))

        self.releaseProject = pilotOptions.get("Project", self.releaseProject)  # default from the code.
        self.log.debug("Release project: %s" % self.releaseProject)

        if "CVMFS_locations" in pilotOptions:
            self.CVMFS_locations = pilotOptions["CVMFS_locations"].replace(" ", "").split(",")
        self.log.debug("CVMFS locations: %s" % self.CVMFS_locations)

    def getPilotOptionsDict(self):
        """
        Get pilot option dictionary by searching paths in a certain order (commands, logging etc.).

        :return: option dict
        :rtype: dict
        """

        return self.getOptionForPaths(self.__getSearchPaths(), self.pilotJSON)

    def __getVO(self):
        """
        Get the VO for which we are running this pilot.
        In case of problems return a value 'unknown", which would get pilot logging
        properties from a Defaults section of the CS.

        :return: VO name
        :rtype: str
        """

        # if the WN is bound to a VO
        if self.wnVO:
            return self.wnVO

        # if env variable "DIRAC_PILOT_VO" is defined
        if os.getenv("DIRAC_PILOT_VO"):
            return os.getenv("DIRAC_PILOT_VO")

        # is there a proxy, and can we get a VO from the proxy?
        cert = os.getenv("X509_USER_PROXY")
        if cert:
            try:
                with open(cert, "rb") as fp:
                    return getVO(fp.read())
            except IOError as err:
                self.log.error("Could not read a proxy, setting vo to 'unknown': %s" % os.strerror(err.errno))
        else:
            self.log.error("Could not locate a proxy via X509_USER_PROXY")

        # is there a token, and can we get a VO from the token?
        # TBD

        return "unknown"

    def __getSearchPaths(self):
        """
        Paths to search for a given VO

        :return: list paths to search in JSON derived dict.
        """

        vo = self.__getVO()
        paths = [
            "/Defaults/Pilot",
            "/%s/Pilot" % self.setup,
            "/%s/Defaults/Pilot" % vo,
            "/%s/%s/Pilot" % (vo, self.setup),
            "/%s/Pilot" % vo,
        ]

        return paths

    @staticmethod
    def getOptionForPaths(paths, inDict):
        """
        Get the preferred option from an input dict passed and a path list. It modifies the inDict.

        :param list paths: list of paths to walk through to get a preferred option. An option found in
        a path which comes later has a preference over options found in earlier paths.
        :param dict inDict:
        :return: dict
        """

        outDict = {}
        for path in paths:
            target = inDict
            for elem in path.strip("/").split("/"):
                target = target.setdefault(elem, {})
            outDict.update(target)
        return outDict

    def __initJSON(self):
        """Retrieve pilot parameters from the content of json file. The file should be something like:

        {
          'DefaultSetup':'xyz',

          'Setups'      :{
                          'SetupName':{
                                        'Commands'           :{
                                                               'GridCEType1' : ['cmd1','cmd2',...],
                                                               'GridCEType2' : ['cmd1','cmd2',...],
                                                               'Defaults'    : ['cmd1','cmd2',...]
                                                              },
                                        'Extensions'         :['ext1','ext2',...],
                                        'ConfigurationServer':'url',
                                        'Version'            :['xyz']
                                        'Project'            :['xyz']
                                      },

                          'Defaults' :{
                                        'Commands'           :{
                                                                'GridCEType1' : ['cmd1','cmd2',...],
                                                                'GridCEType2' : ['cmd1','cmd2',...],
                                                                'Defaults'    : ['cmd1','cmd2',...]
                                                              },
                                        'Extensions'         :['ext1','ext2',...],
                                        'ConfigurationServer':'url',
                                        'Version'            :['xyz']
                                        'Project'            :['xyz']
                                      }
                         }

          'CEs'         :{
                          'ce1.domain':{
                                        'Site'      :'XXX.yyyy.zz',
                                        'GridCEType':'AABBCC'
                                       },
                          'ce2.domain':{
                                        'Site'      :'ZZZ.yyyy.xx',
                                        'GridCEType':'CCBBAA'
                                       }
                         }
        }

        The file must contain at least the Defaults section. Missing values are taken from the Defaults setup."""

        self.__ceType()

        # Commands first
        # FIXME: pilotSynchronizer() should publish these as comma-separated lists. We are ready for that.
        try:
            if isinstance(self.pilotJSON["Setups"][self.setup]["Commands"][self.gridCEType], basestring):
                self.commands = [
                    str(pv).strip()
                    for pv in self.pilotJSON["Setups"][self.setup]["Commands"][self.gridCEType].split(",")
                ]
            else:
                self.commands = [
                    str(pv).strip() for pv in self.pilotJSON["Setups"][self.setup]["Commands"][self.gridCEType]
                ]
        except KeyError:
            try:
                if isinstance(self.pilotJSON["Setups"][self.setup]["Commands"]["Defaults"], basestring):
                    self.commands = [
                        str(pv).strip()
                        for pv in self.pilotJSON["Setups"][self.setup]["Commands"]["Defaults"].split(",")
                    ]
                else:
                    self.commands = [
                        str(pv).strip() for pv in self.pilotJSON["Setups"][self.setup]["Commands"]["Defaults"]
                    ]
            except KeyError:
                try:
                    if isinstance(self.pilotJSON["Setups"]["Defaults"]["Commands"][self.gridCEType], basestring):
                        self.commands = [
                            str(pv).strip()
                            for pv in self.pilotJSON["Setups"]["Defaults"]["Commands"][self.gridCEType].split(",")
                        ]
                    else:
                        self.commands = [
                            str(pv).strip() for pv in self.pilotJSON["Setups"]["Defaults"]["Commands"][self.gridCEType]
                        ]
                except KeyError:
                    try:
                        if isinstance(self.pilotJSON["Defaults"]["Commands"]["Defaults"], basestring):
                            self.commands = [
                                str(pv).strip() for pv in self.pilotJSON["Defaults"]["Commands"]["Defaults"].split(",")
                            ]
                        else:
                            self.commands = [
                                str(pv).strip() for pv in self.pilotJSON["Defaults"]["Commands"]["Defaults"]
                            ]
                    except KeyError:
                        pass
        self.log.debug("Commands: %s" % self.commands)

        # CommandExtensions
        # pilotSynchronizer() can publish this as a comma separated list. We are ready for that.
        try:
            if isinstance(
                self.pilotJSON["Setups"][self.setup]["CommandExtensions"], basestring
            ):  # In the specific setup?
                self.commandExtensions = [
                    str(pv).strip() for pv in self.pilotJSON["Setups"][self.setup]["CommandExtensions"].split(",")
                ]
            else:
                self.commandExtensions = [
                    str(pv).strip() for pv in self.pilotJSON["Setups"][self.setup]["CommandExtensions"]
                ]
        except KeyError:
            try:
                if isinstance(
                    self.pilotJSON["Setups"]["Defaults"]["CommandExtensions"], basestring
                ):  # Or in the defaults section?
                    self.commandExtensions = [
                        str(pv).strip() for pv in self.pilotJSON["Setups"]["Defaults"]["CommandExtensions"].split(",")
                    ]
                else:
                    self.commandExtensions = [
                        str(pv).strip() for pv in self.pilotJSON["Setups"]["Defaults"]["CommandExtensions"]
                    ]
            except KeyError:
                pass
        self.log.debug("Commands extesions: %s" % self.commandExtensions)

        # CS URL(s)
        # pilotSynchronizer() can publish this as a comma separated list. We are ready for that
        try:
            if isinstance(
                self.pilotJSON["ConfigurationServers"], basestring
            ):  # Generic, there may also be setup-specific ones
                self.configServer = ",".join(
                    [str(pv).strip() for pv in self.pilotJSON["ConfigurationServers"].split(",")]
                )
            else:  # it's a list, we suppose
                self.configServer = ",".join([str(pv).strip() for pv in self.pilotJSON["ConfigurationServers"]])
        except KeyError:
            pass
        try:  # now trying to see if there is setup-specific ones
            if isinstance(
                self.pilotJSON["Setups"][self.setup]["ConfigurationServer"], basestring
            ):  # In the specific setup?
                self.configServer = ",".join(
                    [str(pv).strip() for pv in self.pilotJSON["Setups"][self.setup]["ConfigurationServer"].split(",")]
                )
            else:  # it's a list, we suppose
                self.configServer = ",".join(
                    [str(pv).strip() for pv in self.pilotJSON["Setups"][self.setup]["ConfigurationServer"]]
                )
        except KeyError:  # and if it doesn't exist
            try:
                if isinstance(
                    self.pilotJSON["Setups"]["Defaults"]["ConfigurationServer"], basestring
                ):  # Is there one in the defaults section?
                    self.configServer = ",".join(
                        [
                            str(pv).strip()
                            for pv in self.pilotJSON["Setups"]["Defaults"]["ConfigurationServer"].split(",")
                        ]
                    )
                else:  # it's a list, we suppose
                    self.configServer = ",".join(
                        [str(pv).strip() for pv in self.pilotJSON["Setups"]["Defaults"]["ConfigurationServer"]]
                    )
            except KeyError:
                pass
        self.log.debug("CS list: %s" % self.configServer)

        # Version
        # There may be a list of versions specified (in a string, comma separated). We just want the first one.
        dVersion = None
        try:
            dVersion = [dv.strip() for dv in self.pilotJSON["Setups"][self.setup]["Version"].split(",", 1)]
        except KeyError:
            try:
                dVersion = [dv.strip() for dv in self.pilotJSON["Setups"]["Defaults"]["Version"].split(",", 1)]
            except KeyError:
                self.log.warn("Could not find a version in the JSON file configuration")
        if dVersion is not None:
            self.releaseVersion = str(dVersion[0])
        self.log.debug("Version: %s -> %s" % (dVersion, self.releaseVersion))

        try:
            self.releaseProject = str(self.pilotJSON["Setups"][self.setup]["Project"])
        except KeyError:
            try:
                self.releaseProject = str(self.pilotJSON["Setups"]["Defaults"]["Project"])
            except KeyError:
                pass
        self.log.debug("Release project: %s" % self.releaseProject)

    def __ceType(self):
        """
        Set CE type and setup.
        """
        self.log.debug("CE name: %s" % self.ceName)
        if self.ceName:
            # Try to get the site name and grid CEType from the CE name
            # GridCEType is like "CREAM" or "HTCondorCE" not "InProcess" etc
            try:
                self.site = str(self.pilotJSON["CEs"][self.ceName]["Site"])
            except KeyError:
                pass
            try:
                if not self.gridCEType:
                    # We don't override a grid CEType given on the command line!
                    self.gridCEType = str(self.pilotJSON["CEs"][self.ceName]["GridCEType"])
            except KeyError:
                pass
            # This LocalCEType is like 'InProcess' or 'Pool' or 'Pool/Singularity' etc.
            # It can be in the queue and/or the CE level
            try:
                self.ceType = str(self.pilotJSON["CEs"][self.ceName]["LocalCEType"])
            except KeyError:
                pass
            try:
                self.ceType = str(self.pilotJSON["CEs"][self.ceName][self.queueName]["LocalCEType"])
            except KeyError:
                pass

                self.log.debug("Setup: %s" % self.setup)
        self.log.debug("GridCEType: %s" % self.gridCEType)
        if not self.setup:
            # We don't use the default to override an explicit value from command line!
            try:
                self.setup = str(self.pilotJSON["DefaultSetup"])
            except KeyError:
                pass
