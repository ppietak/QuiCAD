# What a model's code may touch. A model is Python, and it is written by an AI
# or brought in from a file, so it runs fenced in: it can read and write inside
# its own project (and the temporary folder), read what Python itself needs,
# and nothing else. No other programs, no network, nothing of the user's home.
#
# The fence is Python's audit hook, set in the process that runs one job and
# then ends. It stops a mistaken or misled model, on every system. It is not
# proof against code written to break out of it; where the system has a real
# sandbox (macOS), the whole worker also runs inside that: see app/builder.py.

import os
import sys
import sysconfig
import tempfile

# Never allowed from a model: starting programs, talking to the network, loading foreign code.
FORBIDDEN = {
    "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.kill", "os.killpg",
    "os.startfile", "os.add_dll_directory", "subprocess.Popen", "pty.spawn", "_winapi.CreateProcess",
    "socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
    "socket.gethostbyaddr", "socket.getnameinfo", "urllib.Request", "http.client.connect", "webbrowser.open",
    "ftplib.connect", "smtplib.connect", "imaplib.open", "poplib.connect", "nntplib.connect",
    "telnetlib.Telnet.open", "ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle", "signal.pthread_kill",
}
FORBIDDEN_FAMILIES = ("winreg.",)

# These change what their path arguments name.
CHANGES = {
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.link",
    "os.symlink", "os.utime", "os.chflags", "os.setxattr", "os.removexattr", "os.mkfifo", "os.mknod",
    "shutil.rmtree", "shutil.move", "shutil.copytree", "shutil.copymode", "shutil.copystat", "shutil.chown",
    "shutil.make_archive", "shutil.unpack_archive", "tempfile.mkstemp", "tempfile.mkdtemp",
}
# These only look.
LOOKS = {"os.listdir", "os.scandir", "glob.glob", "os.walk"}

WRITING = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _under(path, roots):
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def _project_of(req):
    """The project a job belongs to: its working folder, or the one that holds the file it is about."""
    if req.get("cwd"):
        return req["cwd"]
    if req.get("path"):
        return os.path.dirname(os.path.dirname(os.path.abspath(req["path"])))
    return None


def install(req):
    """Fence in the rest of this process for the job `req`. There is no way back, so only a
    process that ends with its job may call this."""
    project = _project_of(req)
    temporary = _real(tempfile.gettempdir())
    writable = [temporary] + ([_real(project)] if project else [])
    home = _real(os.path.expanduser("~"))
    # Under the home folder only these can be read; outside it, anything the system lets anyone read.
    readable = writable + sorted({_real(place) for place in [
        os.path.dirname(os.path.abspath(__file__)), sys.prefix, sys.base_prefix, sys.exec_prefix,
        sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib"),
        os.path.join(home, "Library", "Fonts"), os.path.join(home, ".fonts"), os.path.join(home, ".local", "share", "fonts"),
    ] if place})
    devices = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty", os.devnull)

    def refuse(what):
        raise PermissionError(f"not allowed in a model: {what}. A model can only use files of its own project.")

    def path_of(value):
        if isinstance(value, int) or value is None:
            return None  # an open descriptor: whoever opened it was checked then
        try:
            value = os.fspath(value)
        except TypeError:
            return None
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        return _real(value) if value else None

    def may_write(value, what):
        path = path_of(value)
        if path is not None and path not in devices and not _under(path, writable):
            refuse(f"{what} {path}")

    def may_read(value, what):
        path = path_of(value)
        if path is not None and _under(path, [home]) and not _under(path, readable):
            refuse(f"{what} {path}")

    def hook(event, args):
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writes = any(letter in mode for letter in "wax+") if isinstance(mode, str) else bool((flags or 0) & WRITING)
            (may_write if writes else may_read)(path, "writing" if writes else "reading")
        elif event in CHANGES:
            for value in args:
                may_write(value, "changing")
        elif event == "shutil.copyfile":
            may_read(args[0], "reading")
            may_write(args[1], "writing")
        elif event in LOOKS:
            may_read(args[0] if args else None, "listing")
        elif event in FORBIDDEN or event.startswith(FORBIDDEN_FAMILIES):
            refuse({"subprocess.Popen": "starting a program", "os.system": "starting a program"}.get(event)
                   or ("the network" if event.split(".")[0] in ("socket", "urllib", "http", "ftplib", "smtplib") else event))

    sys.addaudithook(hook)
