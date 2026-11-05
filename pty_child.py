"""Acquire a controlling TTY without using preexec_fn in the threaded parent."""
import fcntl
import os
import sys
import termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.tcsetpgrp(0, os.getpgrp())
os.execvp(sys.argv[1], sys.argv[1:])
