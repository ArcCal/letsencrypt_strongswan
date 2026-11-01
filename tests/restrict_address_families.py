#!/usr/bin/env python3
"""Run a command with socket-family restrictions from a systemd drop-in.

Used only in disposable Linux integration containers without a systemd manager.
This tests RestrictAddressFamilies, not the complete systemd sandbox.
"""
import ctypes
import errno
import os
from pathlib import Path
import re
import socket
import sys


class Argument(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def main():
    config, *command = sys.argv[1:]
    names = re.search(r'^RestrictAddressFamilies=(.+)$', Path(config).read_text(), re.M).group(1).split()
    allowed = {int(getattr(socket, name)) for name in names}
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Argument)]
    library.seccomp_rule_add_array.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('Could not create seccomp context')
    try:
        for name in (b'socket', b'socketpair'):
            number = library.seccomp_syscall_resolve_name(name)
            if number < 0:
                raise RuntimeError('Unknown socket syscall')
            for family in range(128):
                if family in allowed:
                    continue
                compare = Argument(0, 4, family, 0)  # SCMP_CMP_EQ, argument 0
                result = library.seccomp_rule_add_array(
                    context, 0x00050000 | errno.EAFNOSUPPORT, number, 1, ctypes.byref(compare))
                if result:
                    raise RuntimeError(f'Could not add seccomp rule: {result}')
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), 'prctl failed')
        result = library.seccomp_load(context)
        if result:
            raise RuntimeError(f'Could not load seccomp filter: {result}')
    finally:
        library.seccomp_release(context)
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
