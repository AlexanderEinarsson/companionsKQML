#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 3.6
# @Filename: companions_connect.py
# @Author:   Samuel Hill
# @Email:    samuelhill2022@u.northwestern.edu
# @Date:     2019-11-07 15:17:23
# @Last Modified by:   Samuel Hill
# @Last Modified time: 2020-03-01 06:10:01

"""Launching and port reading mechanism for Companions

Trying to deal with all connection possibilities:
 - launch companions (exe) from python <- check portnum file (pid from launch)
 - launch pythonian from companions <- pass in arguments with port number
 - companions running:
   - python connect to exe <- check portnum, look for pid in system
   - python connect to local dev
   - python connect to remote <- no need to check pid
 - NOT: pythonian already running... connect to companions

Attributes:
    LOCALHOST (str): 'localhost' for clean use of the name localhost
    LOCALHOST_DEFS (TYPE): the list of all canonical localhost values
    PORTNUM (str): 'portnum.dat', the file generated by companions
"""

import sys
from argparse import ArgumentParser, ArgumentTypeError
from ipaddress import ip_address
from json import loads as load_dict
from pathlib import Path
from subprocess import Popen
from time import sleep
from typing import Optional
from psutil import disk_partitions, process_iter
from pythonian import Pythonian

__author__ = "Samuel Hill"
__copyright__ = "Copyright 2019, Samuel Hill."
__credits__ = ["Samuel Hill"]

PORTNUM = 'portnum.dat'
LOCALHOST = 'localhost'
LOCALHOST_DEFS = [LOCALHOST, '127.0.0.1', '::1']
COMPANIONS_EXES = ['CompanionsMicroServer64.exe', 'CompanionsServer64.exe']


def context_launch(companions_path: str,
                   exe_name: str = 'CompanionsMicroServer64.exe',
                   verify: bool = False):
    """Runs the executable on companions_path with name exe_name. Also gets
    the portnum.dat file generated by the exe in the same directory and
    passes that port number in to a Pythonian agent.

    Args:
        companions_path (str): path to the companions exe
        exe_name (str, optional): name of companions exe
        verify (bool, optional): whether or not to strictly enforce that the
            pid's match between the process called and the value found in the
            portnum file
    """
    companions_path = Path(companions_path)
    exe_path = companions_path / exe_name
    portnum_path = companions_path / PORTNUM
    if portnum_path.exists():  # not needed in Python 3.8
        portnum_path.unlink()  # missing_ok keyword handles nonexistant files
    # with only calls wait on exit, not terminate...
    # with Popen(str(exe_path)) as proc:  # safely contain process call...
    #     while not portnum_path.exists():
    #         sleep(1)
    #     port_dict = read_portfile(portnum_path)
    #     # Once Pythonian is shutdown the process should terminate
    #     Pythonian(port=get_port(port_dict, proc.pid, verify))
    proc = Popen(str(exe_path))
    while not portnum_path.exists():
        sleep(1)
    Pythonian(port=get_port(portnum_path, proc.pid, verify))
    # ONLY CALL THIS ON PYTHONIAN SHUT DOWN
    # NEED MODIFIED PYTHONIAN TO HANDLE THIS
    # proc.terminate()


# @classmethod
# def arg_parser(cls: Pythonian) -> Pythonian:
def arg_parser() -> dict:
    """Uses ArgumentParser to parse the args that Pythonian is called with.
    Additional benefit of searching your system for a running Companion
    (given a number of assumptions: qrg installed on the root of some drive on
    the system or the home directory, CompanionsMicroServer64 before
    CompanionsServer64, allegro for local dev, find running exe first then find
    local dev, etc.) if no port is specified. If no running companion is found,
    uses default Pythonian values. The url - host, listener_port, and debug
    arguments all also fall back to Pythonian defaults. The additional argument
    -v for verify_pids will assert that the pid's match between what is found
    on the system and what is put in the file.

    Returns:
        dict: kwargs for Pythonian
    """
    _, *args = sys.argv  # ignore name of file...
    # args = sys.argv[1:]  # ignore name of file...
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
                             '(created by either running companions locally or'
                             'in an exe) against the pid found on the running '
                             'process where the portnum.dat file was found')
    args = parser.parse_args(args)
    kwargs = {}  # repack arguments for a non-default interrupting call
    if args.debug:
        kwargs['debug'] = args.debug
    if args.listener_port:
        kwargs['listener_port'] = args.listener_port
    host_local = True  # default to hosting locally...
    if args.url:
        kwargs['host'] = args.url
        host_local = args.url in LOCALHOST_DEFS
    if args.port:
        kwargs['port'] = args.port
    # if port is missing... (and url is local, by declaration or by default)
    if not args.port and host_local:
        port = check_for_companions(args.verify_port)
        if port:  # if port found on a running companions agent, use it...
            kwargs['port'] = port
    return kwargs  # if no port passed or found, use default
    # return cls(**kwargs)


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
    potential_port = None
    processes = process_iter(attrs=['pid', 'name', 'exe'])
    # search for running companions executables
    companion = None
    for name in COMPANIONS_EXES:
        process = find_named_process(name, processes)
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
    allegro = find_named_process('allegro.exe', processes)
    if qrg_root and allegro:
        portnum_path = qrg_root / 'companions' / 'v1' / PORTNUM
        potential_port = get_port(portnum_path, allegro['pid'], verify)
    # Could have not found anything, in this case we return None by nature of
    # potential_port not having had a new value assigned
    return potential_port


def find_named_process(name: str, processes: dict) -> Optional[dict]:
    """Searches for the named process in a list of running processes

    Args:
        name (str): process name you are searching for
        processes (dict): list of processes to be searched over

    Returns:
        Optional[dict]: the process (name, pid, and exe) as a dict if found,
            otherwise None
    """
    processes = [p.info for p in processes if name in p.info['name']]
    return processes[0] if processes else None


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