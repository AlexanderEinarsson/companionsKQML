#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 3.6
# @Filename:    companionsKQMLModule.py
# @Author:      Samuel Hill
# @Date:        2020-01-29 14:48:19
# @Last Modified by:   Samuel Hill
# @Last Modified time: 2020-03-06 11:32:10

"""CompanionsKQMLModule, Override of KQMLModule for creation of Companions
agents. Adds a KQML socket server that is kept alive in a thread for
continuous communication between Companions and your python agents.

Attributes:
    COMPANIONS_EXES (list): list of common companions executable names
    KQMLType (TypeVar): simplified type for KQML, includes list, tokens, and
        strings
    LOCALHOST (str): 'localhost'
    LOCALHOST_DEFS (list): list of common localhost equivalents
    LOGGER (logging): The logger (from logging) to handle debugging
    PORTNUM (str): 'portnum.dat' - name of file generated by Companions on
        startup of it's own KQML socket server
"""

from argparse import ArgumentParser, ArgumentTypeError
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BufferedReader, BufferedWriter
from ipaddress import ip_address
from json import loads as load_dict
from logging import getLogger, DEBUG, INFO
from pathlib import Path
from socket import socket, SocketIO, gethostname, SOL_SOCKET, SO_REUSEADDR, \
     SHUT_RDWR
from subprocess import Popen
from sys import argv as system_argument_list
from threading import Thread
from time import sleep
from typing import Optional, Any, TypeVar
# non-system, pip installs
from dateutil.relativedelta import relativedelta
from kqml import KQMLModule, KQMLReader, KQMLPerformative, KQMLList, \
     KQMLDispatcher, KQMLToken, KQMLString
from psutil import disk_partitions, process_iter

PORTNUM = 'portnum.dat'
LOCALHOST = 'localhost'
LOCALHOST_DEFS = [LOCALHOST, '127.0.0.1', '::1']
COMPANIONS_EXES = ['CompanionsMicroServer64.exe', 'CompanionsServer64.exe']
KQMLType = TypeVar('KQML_TYPE', KQMLList, KQMLToken, KQMLString)

LOGGER = getLogger(__name__)


###############################################################################
#                  Modified KQMLModule with threaded server                   #
###############################################################################

# pylint: disable=too-many-instance-attributes
#   We need to keep track of a threaded socket server, a socket connection to
#   companions, and deal with overriding from KQMLModule appropriately
class CompanionsKQMLModule(KQMLModule):
    """KQMLModule override to allow for continuous back and forth communication
    from the running companions agent (facilitator) and this agent.

    Attributes:
        debug (bool): helps set the debug level for the loggers accross modules
        dispatcher (KQMLDispatcher): Dispatcher to be used (from KQMLModule),
            calls on appropriate functions based on incoming messages,
            need to keep track of it for proper shutdown
        host (str): The host of Companions (localhost or an ip address)
        listen_socket (socket): Socket object the listener will control,
            receives incoming messages from Companions
        listener (Thread): Thread running the socket listening loop, calls the
            dispatcher as well.
        listener_port (int): port number you want to host the listener on
        local_out (BufferedWriter): Connection to the listener socker server
           output, used to send messages on the listener port for Companions
           to pick up on.
        name (str): Name of this agent (module), used in registration so this
            should be set to a new name for each new agent. Currently all
            instances of this class will have the same name as they are the
            same type of agent.
        num_subs (int): The number of subscriptions that the agent has (only
            used later in Pythonian)
        out (BufferedWriter): Connection to the Companions KQML socket server,
            created from send_socket, used by send
        port (int): port number that Companions is hosted on
        ready (bool): Boolean that controls the threads looping, overwrites the
            ready function from KQMLModule
        reply_id_counter (int): From KQMLModule, used in send_with_continuation
            adds reply-with and the appropriate reply id
        send_socket (socket): Socket that will connect to Companions for
            sending messages. Need to keep track of it to properly close itself
            initializes to None and only has a socket after calling connect
        starttime (datetime): the time at which this agent started, used for
            updating running status in Companions
        state (str): the state this agent is in, used for updating running
            status in Companions
    """

    name = 'CompanionsKQMLModule'

    # pylint: disable=super-init-not-called
    #   We are rewriting the KQMLModule...
    def __init__(self, host: str = 'localhost', port: int = 9000,
                 listener_port: int = 8950, debug: bool = False):
        """Override of KQMLModule init to add turn it into a KQML socket server

        Args:
            host (str, optional): the host location to connect to via sockets
            port (int, optional): the port on the host to connect to
            listener_port (int, optional): the port this class will host its
                KQML socket server from (the connection end that dispatches
                requests as needed)
            debug (bool, optional): Whether to set the level of the logger to
                DEBUG or INFO - silencing debug errors and only showing needed
                information.
        """
        # OUTPUTS
        assert valid_ip(host), 'Host must be local or a valid ip address'
        self.host = host
        assert valid_port(port), \
            'port must be valid port number (1024-65535)'
        self.port = port
        self.send_socket = None
        self.out = None
        # INPUTS
        assert valid_port(listener_port), \
            'listener_port must be a valid port number (1024-65535)'
        self.listener_port = listener_port
        self.dispatcher = None
        self.listen_socket = socket()
        self.listen_socket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        self.listen_socket.bind(('', self.listener_port))
        self.listen_socket.listen(10)
        self.local_out = None
        self.ready = True
        self.listener = Thread(target=self.listen, args=[])
        # FROM KQMLModule
        self.reply_id_counter = 1
        # UPDATES
        self.starttime = datetime.now()
        self.state = 'idle'
        self.num_subs = 0
        # LOGGING / DEBUG
        self.debug = debug
        if self.debug:
            LOGGER.setLevel(DEBUG)
        else:
            LOGGER.setLevel(INFO)
        # REGISTER AND START LISTENING
        LOGGER.info('Starting listener (KQML socket server)...')
        self.listener.start()
        self.register()

    @classmethod
    # pylint: disable=too-many-arguments
    # We have 5 arguments plus the class to be created...
    # 4 arguments from init (keeping init clean and low on arguments),
    # 1 extra for controlling the check for companions function...
    def init_check_companions(cls, host: str = None, port: int = None,
                              listener_port: int = None, debug: bool = None,
                              verify_port: bool = False):
        """Helper method for constructing an agent, with a special helper
        function if you are running companions on the same machine as this
        agent (judged by connecting to localhost), without overwriting the
        default values in init. When the companion is running on the same
        system as the python agent we check to see if an expected process is
        running, and if so we look for the listed port number that the process
        publishes on startup. The check for companions will prioritize
        executables before looking for something running from source
        (CompanionsMicroServer64 before CompanionsServer64, allegro for local
        development with a qrg directory installed at the root of some drive on
        the system or in the home directory) and will return the first port
        found (if multiple companions are running). If nothing is found, the
        default value is relied on. If nothing is running but an old port
        number is found, you won't connect either way as a companion isn't
        running - it just might attempt a non-default port. As such, this can
        be essentially used in place on regular init.

        Args:
            host (str, optional): the host value to pass to init, falls back to
                init defaults.
            port (int, optional): the port value to pass to init, falls back to
                init defaults.
            listener_port (int, optional): the listener_port value to pass to
                init, falls back to init defaults.
            debug (bool, optional): the debug value to pass to init, falls back
                to init defaults.
            verify_port (bool, optional): whether or not to verify the port
                number by checking the pid in the portnum.dat file (created by
                either running companions locally or in an exe) against the pid
                found on the running process where the portnum.dat file was
                found

        Returns:
            cls: instantiated cls object
        """
        kwargs = {}  # repack arguments for a non-default interrupting call
        if host:
            kwargs['host'] = host
        if port:
            kwargs['port'] = port
        if listener_port:
            kwargs['listener_port'] = listener_port
        if debug:
            kwargs['debug'] = debug
        # If no port was passed in and either no host or localhost (no host
        # would default to local)...
        if not port and (True if not host else host in LOCALHOST_DEFS):
            port = check_for_companions(verify_port)
            if port:
                kwargs['port'] = port
        return cls(**kwargs)

    @classmethod
    def parse_command_line_args(cls, argv: list = None):
        """Uses ArgumentParser to parse the args that this is called with.
        Additional benefit of searching your system for a running Companion if
        no port is specified and host is local (defaults to local). If no
        running companion is found use the default values from init. The
        additional argument -v for verify_pids will assert that the pid's match
        between what is found on the system and what is put in the file.

        Returns:
            cls: instantiated cls object

        Args:
            argv (list, optional): argument list (typically from sys.argv)
        """
        if not argv:
            argv = system_argument_list
        _, *args = argv  # ignore name of file...
        parser = ArgumentParser(description='Run Pythonian agent.')
        parser.add_argument('-u', '--url', type=valid_ip,
                            help='url where companions kqml server is hosted')
        parser.add_argument('-p', '--port', type=valid_port,
                            help='port companions kqml server is open on')
        parser.add_argument('-l', '--listener_port', type=valid_port,
                            help='port pythonian kqml server is open on')
        parser.add_argument('-d', '--debug', action='store_true',
                            help='whether or not to log debug messages')
        parser.add_argument('-v', '--verify_port', action='store_true',
                            help='whether or not to verify the port number by '
                                 'checking the pid in the portnum.dat file '
                                 '(created by either running companions '
                                 'locally or in an exe) against the pid found '
                                 'on the running process where the portnum.dat'
                                 ' file was found')
        args = parser.parse_args(args)
        return cls.init_check_companions(host=args.url, port=args.port,
                                         listener_port=args.listener_port,
                                         debug=args.debug,
                                         verify_port=args.verify_port)

    # OUTPUT FUNCTIONS (OVERRIDES):

    # pylint: disable=arguments-differ
    def connect(self):
        """Rewrite of KQMLModule connect, only handles send_socket and output
        connections"""
        try:
            self.send_socket = socket()
            self.send_socket.connect((self.host, self.port))
            socket_write = SocketIO(self.send_socket, 'w')
            self.out = BufferedWriter(socket_write)
        except OSError as error_msg:
            LOGGER.error('Connection failed: %s', error_msg)
        # Verify that you can send messages...
        assert self.out is not None, \
            'Connection formed but output (%s) not set.' % (self.out)

    def send(self, msg: KQMLPerformative):
        """Override of send from KQMLModule, opens and closes socket around
        send for proper signaling to Companions

        Args:
            msg (KQMLPerformative): message that you are sending to Companions
        """
        self.connect()
        self.send_generic(msg, self.out)
        self.send_socket.shutdown(SHUT_RDWR)
        self.send_socket.close()
        self.send_socket = None
        self.out = None

    def send_on_local_port(self, msg: KQMLPerformative):
        """Sends a message on the local_out, i.e. sends a message on the
        listener_port. This is used for some specific functions that are not
        meant to be handled as a kqml performative.

        Args:
            msg (KQMLPerformative): message to be sent
        """
        self.send_generic(msg, self.local_out)

    def reply_on_local_port(self, msg: KQMLPerformative,
                            reply_msg: KQMLPerformative):
        """Replies to a message on the local port (listener port)

        Args:
            msg (KQMLPerformative): message to reply to
            reply_msg (KQMLPerformative): message to reply with
        """
        sender = msg.get('sender')
        if sender is not None:
            reply_msg.set('receiver', sender)
        reply_with = msg.get('reply-with')
        if reply_with is not None:
            reply_msg.set('in-reply-to', reply_with)
        self.send_on_local_port(reply_msg)

    @staticmethod
    def send_generic(msg: KQMLPerformative, out: BufferedWriter):
        """Basic send mechanism copied (more or less) from pykqml. Writes the
        msg as a string to the output buffer then flushes it.

        Args:
            msg (KQMLPerformative): Message to be sent
            out (BufferedWriter): The output to write to, needed for sending to
                Companions and sending on our own port.
        """
        LOGGER.debug('Sending: %s', msg)
        try:
            msg.write(out)
        except IOError:
            LOGGER.error('IOError during message sending')
        out.write(b'\n')
        out.flush()

    # INPUT FUNCTIONS (OVERRIDE AND ADDITION):

    def listen(self):
        """Socket server, listens for new KQML messages and dispatches them
        accordingly.

        Doesn't necessarily need threading; Could just start dispatcher and
        after it returns accept next connection. This couldn't handle loads of
        inputs while being bogged down processing. To avoid this issue we
        thread the dispatching so the functions that get called are run in a
        separate Thread. We're using ThreadPoolExecutor because sockets use io,
        io is blocking and threads allow you to not block.
        """
        with ThreadPoolExecutor(max_workers=5) as executor:
            while self.ready:
                connection, _ = self.listen_socket.accept()
                LOGGER.debug('Received connection: %s', connection)
                socket_write = SocketIO(connection, 'w')
                self.local_out = BufferedWriter(socket_write)
                socket_read = SocketIO(connection, 'r')
                read_input = KQMLReader(BufferedReader(socket_read))
                self.dispatcher = KQMLDispatcher(self, read_input, self.name)
                LOGGER.debug('Starting dispatcher: %s', self.dispatcher)
                executor.submit(self.dispatcher.start)
                self.state = 'dispatching'

    def receive_eof(self):
        """Override of KQMLModule, shuts down the dispatcher after receiving
        the end of file (eof) signal. This happens after every message...
        """
        LOGGER.debug('Closing connection on dispatcher: %s', self.dispatcher)
        self.dispatcher.shutdown()
        self.dispatcher = None
        self.state = 'idle'

    # OVERRIDES TO KQMLModule:

    def start(self):
        pass

    def connect1(self):
        pass

    def exit(self, n: int = 0):
        """Override of KQMLModule; Closes this agent, shuts down the threaded
        execution loop (by turning off the ready flag), shuts the dispatcher
        down (if running), and then joins any running threads...

        Args:
            n (int, optional): the value to pass along to sys.exit
        """
        LOGGER.info('Shutting down agent: %s', self.name)
        self.ready = False  # may need to wait for threads to stop...
        if self.dispatcher is not None:
            self.dispatcher.shutdown()
        self.listener.join()

    # COMPANIONS SPECIFIC OVERRIDES:

    def register(self):
        """Override of KQMLModule, registers this agent with Companions"""
        LOGGER.info('Registering...')
        registration = (
            f'(register :sender {self.name} :receiver facilitator :content '
            f'("socket://{self.host}:{self.listener_port}" nil nil '
            f'{self.listener_port}))'
        )
        self.send(performative(registration))

    def receive_other_performative(self, msg: KQMLPerformative):
        """Override of KQMLModule default... ping isn't currently supported by
        pykqml so we handle other to catch ping and otherwise throw an error.

        Arguments:
            msg (KQMLPerformative): other type of performative, if ping we
                reply with a ping update otherwise error
        """
        if msg.head() == 'ping':
            LOGGER.info('Receive ping... %s', msg)
            reply_content = (
                f'(update :sender {self.name} :content (:agent {self.name} '
                f':uptime {self.uptime()} :status :OK :state {self.state} '
                f':machine {gethostname()} :subscriptions {self.num_subs}))'
            )
            self.reply_on_local_port(msg, performative(reply_content))
        else:
            self.error_reply(msg, f'unexpected performative: {msg}')

    # Everything else (reply, error_reply, handle_exceptions,
    #   send_with_continuation, subscribe_request, subscribe_tell,
    #   and ALL the remaining receive_* functions) is fine as is

    # HELPERS:

    def uptime(self) -> str:
        """Cyc-style time since start. Using the python-dateutil library to do
        simple relative delta calculations for the uptime.

        Returns:
            str: string of the form
                     '(years months days hours minutes seconds)'
                 where years, months, days, etc are the uptime in number of
                 years, months, days, etc.
        """
        time_list = ['years', 'months', 'days', 'hours', 'minutes', 'seconds']
        diff = relativedelta(datetime.now(), self.starttime)
        time_diffs = [getattr(diff, time_period) for time_period in time_list]
        return f'({" ".join(map(str, time_diffs))})'

    def response_to_query(self, msg: KQMLPerformative,
                          content: KQMLPerformative, results: Any,
                          response_type: str):
        """Based on the response type, will create a properly formed reply
        with the results either input as patterns or bound to the arguments
        from the results. The reply is a tell which is then sent to Companions.

        Goes through the arguments and the results together to either bind a
        argument to the result or simple return the result in the place of that
        argument. The reply content is filled with these argument/result lists
        (they are listified before appending) before being added to the tell
        message and subsequently sent off to Companions.

        Arguments:
            msg (KQMLPerformative): the message being passed along to reply
            content (KQMLPerformative): query, starts with a predicate and the
                remainder is the arguments
            results (Any): The results of performing the query
            response_type (str): the given response type, if it is not given or
                is given to be pattern, the variable will be set to True,
                otherwise False
        """
        LOGGER.debug('Responding to query: %s, %s, %s', msg, content, results)
        response_type = response_type is None or response_type == ':pattern'
        reply_content = KQMLList(content.head())
        results_list = results if isinstance(results, list) else [results]
        result_index = 0
        arg_len = len(content.data[1:])
        for i, each in enumerate(content.data[1:]):
            # if argument is a variable, replace in the pattern or bind
            if str(each[0]) == '?':
                # if last argument and there's still more in results
                if i == arg_len and result_index < len(results_list)-1:
                    pattern = results_list[result_index:]  # get remaining list
                else:
                    pattern = results_list[result_index]
                reply_with = pattern if response_type else (each, pattern)
                reply_content.append(listify(reply_with))
                result_index += 1
            # if not a variable, replace in the pattern. Ignore for bind
            elif response_type:
                reply_content.append(each)
        # no need to wrap reply_content in parens, KQMLList will do that for us
        reply_msg = f'(tell :sender {self.name} :content {reply_content})'
        self.reply(msg, performative(reply_msg))


###############################################################################
#           Companions controlling extension of kqml server version           #
###############################################################################

class ControlledCompanionsKQMLModule(CompanionsKQMLModule):
    """Version of the CompanionsKQMLModule that will launch and shut down its
    own Companions exe for connecting and querying.

    Attributes:
        companions_process (Popen): the running Companions exe
    """

    # TODO: need default for exe_path
    def __init__(self, exe_path: str, exe_name: str = COMPANIONS_EXES[0],
                 verify_port: bool = True, **kwargs):
        """Launches a companions exe and uses that for connecting to Companions

        Args:
            exe_path (str): path to the companions executable
            exe_name (str): name of the executable
            verify_port (bool, optional): Whether or not to verify that the
                port associated with your Companion is the one just opened on
                the exe
            **kwargs: the remaining kwargs to be passes to CompanionsKQMLModule
        """
        exe_path = Path(exe_path)
        portnum_path = exe_path / PORTNUM
        exe_location = exe_path / exe_name
        if portnum_path.exists():  # not needed in Python 3.8
            portnum_path.unlink()  # missing_ok key handles nonexistant files
        self.companions_process = Popen(str(exe_location))
        LOGGER.info('Launched companions: %s', self.companions_process)
        while not portnum_path.exists():
            sleep(1)
        kwargs['port'] = get_port(portnum_path, self.companions_process.pid,
                                  verify_port)
        super().__init__(**kwargs)

    @classmethod
    def parse_command_line_args(cls, argv: list = None):
        if not argv:
            argv = system_argument_list
        _, *args = argv  # ignore name of file...
        parser = ArgumentParser(description='Run Pythonian agent.')
        parser.add_argument('-p', '--port', type=valid_port,
                            help='port companions kqml server is open on')
        parser.add_argument('-l', '--listener_port', type=valid_port,
                            help='port pythonian kqml server is open on')
        parser.add_argument('-e', '--exe_path', type=str,
                            help='path to the executable to be launched')
        parser.add_argument('-n', '--exe_name', type=str,
                            help='name of the executable to be launched')
        parser.add_argument('-d', '--debug', action='store_true',
                            help='whether or not to log debug messages')
        parser.add_argument('-v', '--verify_port', action='store_true',
                            help='whether or not to verify the port number by '
                                 'checking the pid in the portnum.dat file '
                                 '(created by either running companions '
                                 'locally or in an exe) against the pid found '
                                 'on the running process where the portnum.dat'
                                 ' file was found')
        args = parser.parse_args(args)
        kwargs = {}
        if args.port:
            kwargs['port'] = args.port
        if args.listener_port:
            kwargs['listener_port'] = args.listener_port
        if args.exe_path:
            kwargs['exe_path'] = args.exe_path
        if args.exe_name:
            kwargs['exe_name'] = args.exe_name
        if args.debug:
            kwargs['debug'] = args.debug
        if args.verify_port:
            kwargs['verify_port'] = args.verify_port
        return cls(**kwargs)

    def exit(self, n: int = 0):
        """Override of CompanionsKQMLModule, allows for a Companions process
        to be exited on exit of the rest of the system.

        Args:
            n (int, optional): the value to pass along to sys.exit
        """
        super().exit(n)
        if self.companions_process:
            LOGGER.info('Shutting down companions: %s',
                        self.companions_process)
            self.companions_process.terminate()


###############################################################################
#                  KQMLList & KQMLPerformative replacements                   #
###############################################################################

# pylint: disable=too-many-return-statements
# Eight is reasonable in this case, need to break down many data types.
def listify(possible_list: Any) -> KQMLType:
    """Takes in an object and returns it in KQML form.

    Checks if the input is a list, and if so it recurses through all entities
    in the list to further listify them. If the input is not a list but is
    instead a tuple of length 2 we make the assumption that this is a dotted
    pair and construct the KQMLList as such, otherwise we treat this larger
    tuple the same as a list. If the input is a string, we first check that it
    has a space in it (to differentiate facts, strings, and tokens). We then
    check if it is in lisp form (i.e. '(...)') and if so we split every term
    between the parens by the spaces. Otherwise we return the object as a
    KQMLString. In either case, if the string had no spaces in it we return it
    as a KQMLToken. WARNING: This may be an incomplete breakdown of strings.
    Next we check if the input was a dictionary and if so we listify the key
    value pairs, and then make a KQMLList of that overall list of pairs. If the
    input is a bool we return t for True and nil for False. Lastly, if the
    input was nothing else we return the input as a string turned into a
    KQMLToken.

    Arguments:
        possible_list (Any): any input that you want to transform to KQML
            ready data types

    Returns:
        KQMLType
    """
    if isinstance(possible_list, list):
        new_list = [listify(each) for each in possible_list]
        return KQMLList(new_list)
    if isinstance(possible_list, tuple):
        if len(possible_list) == 2:
            car = listify(possible_list[0])
            cdr = listify(possible_list[1])
            return KQMLList([car, KQMLToken('.'), cdr])
        new_list = [listify(each) for each in possible_list]
        return KQMLList(new_list)
    if isinstance(possible_list, str):
        if ' ' in possible_list:
            # WARNING: This may be an incomplete breakdown of strings.
            if possible_list[0] == '(' and possible_list[-1] == ')':
                terms = possible_list[1:-1].split()
                return KQMLList([listify(t) for t in terms])
            return KQMLString(possible_list)
        return KQMLToken(possible_list)
    if isinstance(possible_list, dict):
        return KQMLList([listify(pair) for pair in possible_list.items()])
    if isinstance(possible_list, bool):
        return KQMLToken('t') if possible_list else KQMLToken('nil')
    return KQMLToken(str(possible_list))


def performative(string: str) -> KQMLPerformative:
    """Wrapper for KQMLPerformative.from_string, produces a performative object
    from a KQML string

    Arguments:
        string (str): well formed KQML performative as a string

    Returns:
        KQMLPerformative
    """
    return KQMLPerformative.from_string(string)


###############################################################################
#                         Lisp to Python style helpers                        #
###############################################################################

def convert_to_boolean(to_be_bool: Any) -> bool:
    """Uses some lisp conventions to determine how something should be
    converted to a Boolean. If the KQML element is 'nil' or an empty list then
    this will return False. Otherwise, it returns True.

    Arguments:
        to_be_bool (Any): KQMLToken and KQMLList will be properly converted
            to Lisp style nil, anything else is True.

    Returns:
        bool
    """
    if isinstance(to_be_bool, KQMLToken) and to_be_bool.data == "nil":
        return False
    # pylint: disable=len-as-condition
    # This is an issue that pylint is fixing in release 2.4.0.
    # len(seq) == 0 is okay just len(seq) isn't.
    if isinstance(to_be_bool, KQMLList) and len(to_be_bool) == 0:
        return False
    return True


def convert_to_int(to_be_int: Any) -> int:
    """Gets the data of the KQMLToken and casts it to an int.

    Arguments:
        to_be_int (Any): converts the data in a KQMLToken or KQMLList to an
            int, otherwise we try to pass whatever it is to int() and return
            that - could raise an error...

    Returns:
        int
    """
    if isinstance(to_be_int, KQMLToken):
        return int(to_be_int.data)
    if isinstance(to_be_int, KQMLString):
        return int(to_be_int.data)
    return int(to_be_int)


###############################################################################
#                 Argument parsing & port convenience helpers                 #
###############################################################################

def valid_ip(string: str) -> str:
    """argparse type checking function for ip addresses. Valid if the ip
    address is either localhost or meets either of the ip4 or ip6 standards.

    Args:
        string (str): ip address as a string, usually passed in by arguments

    Returns:
        str: valid ip address

    Raises:
        ArgumentTypeError: If the ip address is not in the ip4 or ip6 format
            then this will fail as it is not a valid ip address for sockets
    """
    if string in LOCALHOST_DEFS:
        return string
    try:
        ip_address(string)
    except ValueError:
        raise ArgumentTypeError(f'{string} is not a valid host (ip address)')
    return string


def valid_port(string: str) -> int:
    """argparse type checking/conversion function for portnumbers. Valid if the
    port number is in the range 1024 to 65535.

    Args:
        string (str): port number as a string, usually passed in by arguments

    Returns:
        int: valid port number

    Raises:
        ArgumentTypeError: If the port number is not in the range 1024 to 65535
            the argparse will fail as it is not a valid port number for sockets
    """
    try:
        port_num = int(string)
    except ValueError:
        raise ArgumentTypeError(f'{port_num} is not a valid port number')
    in_range = 1024 < port_num < 65535
    if not in_range:
        raise ArgumentTypeError(f'{port_num} is not a valid port number')
    return port_num


def check_for_companions(verify: bool = False) -> Optional[int]:
    """A helper function that will check for a running companions executable
    OR for the allegro development environment (plus a qrg directory) and
    try to get it's port number from the port dictionary it creates in
    portnum.dat

    Args:
        verify (bool, optional): whether or not to verify that the companions
            process being looked at has the same pid as the one stored in it's
            port_dict

    Returns:
        Optional[int]: portnum of a running process (if found)
    """
    LOGGER.debug('Checking for companions...')
    potential_port = None
    processes = process_iter(attrs=['pid', 'name', 'exe'])
    # search for running companions executables
    companion = None
    for name in COMPANIONS_EXES:
        process = next((p.info for p in processes if name in p.info['name']),
                       None)  # default value returned if no process found.
        if process:
            companion = process
            break
    if companion:
        portnum_path = Path(companion['exe']).with_name(PORTNUM)
        potential_port = get_port(portnum_path, companion['pid'], verify)
    if potential_port:
        return potential_port
    # search for the qrg directory (in default locations) and a running allegro
    # executable -> doesn't always mean that companions is running
    qrg_root = None
    potential_roots = [Path(disk.mountpoint) for disk in disk_partitions()]
    potential_roots.append(Path.home())
    for root in potential_roots:
        qrg = root / 'qrg'
        if qrg.exists():
            qrg_root = qrg
            break
    # allegro = _find_named_process('allegro.exe', processes)
    allegro = next((p.info for p in processes
                    if 'allegro.exe' in p.info['name']), None)
    if qrg_root and allegro:
        portnum_path = qrg_root / 'companions' / 'v1' / PORTNUM
        potential_port = get_port(portnum_path, allegro['pid'], verify)
    # Could have not found anything, in this case we return None by nature of
    # potential_port not having had a new value assigned
    return potential_port


def get_port(portnum_path: Path, process_pid: int,
             verify: bool = False) -> Optional[int]:
    """Gets the port number from the portnum.dat file as a dict. If verify is
    true the port number is only returned if the pid in the portnum file is a
    match with the process_pid passed in.

    Args:
        portnum_path (Path): path to the port dictionary generated by
            converting portnum.dat - a dictionary with the keys pid and port
            (and associated int values)
        process_pid (int): the pid of the process we are expecting to have
            produced the portnum file (which has been turned into a dict) and
            passed into the port_dict arg
        verify (bool, optional): whether or not to assert that the process_pid
            equals the value stored at the key 'pid' in the port_dict

    Returns:
         Optional[int]: port number found in port_dict (or None if not found,
            or not valid)
    """
    if portnum_path.exists():
        with portnum_path.open() as portnum_file:
            port_dict = load_dict(portnum_file.readline())
        if 'port' not in port_dict:
            return None
        if verify:
            assert 'pid' in port_dict
            assert process_pid == port_dict['pid']
        return port_dict['port']
    return None
