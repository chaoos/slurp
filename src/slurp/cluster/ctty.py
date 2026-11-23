"""CTTY: a python -c that makes the pty on its stdin the controlling terminal of the command it runs (the shells of
the terminal pane, the app under the session holder, a shared terminal); light: the session holder imports it"""

CTTY = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
