#!/usr/bin/env python
# License: GPLv3 Copyright: 2022, Kovid Goyal <kovid at kovidgoyal.net>


import base64
import contextlib
import errno
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios

tty_file_obj = None
echo_on = int('ECHO_ON')
data_dir = shell_integration_dir = ''
request_data = int('REQUEST_DATA')
leading_data = b''
login_shell = os.environ.get('SHELL') or '/bin/sh'
try:
    login_shell = pwd.getpwuid(os.geteuid()).pw_shell
except KeyError:
    pass
export_home_cmd = b'EXPORT_HOME_CMD'
if export_home_cmd:
    HOME = base64.standard_b64decode(export_home_cmd).decode('utf-8')
    os.chdir(HOME)
else:
    HOME = os.path.expanduser('~')


def set_echo(fd, on=False):
    if fd < 0:
        fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    if on:
        new[3] |= termios.ECHO
    else:
        new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSANOW, new)
    return fd, old


def cleanup():
    global tty_file_obj
    if tty_file_obj is not None:
        if echo_on:
            set_echo(tty_file_obj.fileno(), True)
        tty_file_obj.close()
        tty_file_obj = None


def write_all(fd, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    data = memoryview(data)
    while data:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            continue
        if not n:
            break
        data = data[n:]


def dcs_to_kitty(payload, type='ssh'):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    payload = base64.standard_b64encode(payload)
    return b'\033P@kitty-' + type.encode('ascii') + b'|' + payload + b'\033\\'


def send_data_request():
    write_all(tty_file_obj.fileno(), dcs_to_kitty('id=REQUEST_ID:pwfile=PASSWORD_FILENAME:pw=DATA_PASSWORD'))


def debug(msg):
    data = dcs_to_kitty('debug: {}'.format(msg), 'print')
    if tty_file_obj is None:
        with open(os.ctermid(), 'wb') as fl:
            write_all(fl.fileno(), data)
    else:
        write_all(tty_file_obj.fileno(), data)


def apply_env_vars(raw):
    global login_shell

    def process_defn(defn):
        parts = json.loads(defn)
        if len(parts) == 1:
            key, val = parts[0], ''
        else:
            key, val, literal_quote = parts
            if not literal_quote:
                val = os.path.expandvars(val)
        os.environ[key] = val

    for line in raw.splitlines():
        val = line.split(' ', 1)[-1]
        if line.startswith('export '):
            process_defn(val)
        elif line.startswith('unset '):
            os.environ.pop(json.loads(val)[0], None)
    login_shell = os.environ.pop('KITTY_LOGIN_SHELL', login_shell)


def move(src, base_dest):
    for x in os.listdir(src):
        path = os.path.join(src, x)
        dest = os.path.join(base_dest, x)
        if os.path.islink(path):
            try:
                os.unlink(dest)
            except EnvironmentError:
                pass
            os.symlink(os.readlink(path), dest)
        elif os.path.isdir(path):
            if not os.path.exists(dest):
                os.makedirs(dest)
            move(path, dest)
        else:
            shutil.move(path, dest)


def compile_terminfo(base):
    try:
        tic = shutil.which('tic')
    except AttributeError:
        # python2
        for x in os.environ.get('PATH', '').split(os.pathsep):
            q = os.path.join(x, 'tic')
            if os.access(q, os.X_OK) and os.path.isfile(q):
                tic = q
                break
        else:
            tic = ''
    if not tic:
        return
    tname = '.terminfo'
    q = os.path.join(base, tname, '78', 'xterm-kitty')
    if not os.path.exists(q):
        try:
            os.makedirs(os.path.dirname(q))
        except EnvironmentError as e:
            if e.errno != errno.EEXIST:
                raise
        os.symlink('../x/xterm-kitty', q)
    if os.path.exists('/usr/share/misc/terminfo.cdb'):
        # NetBSD requires this
        os.symlink('../../.terminfo.cdb', os.path.join(base, tname, 'x', 'xterm-kitty'))
        tname += '.cdb'
    os.environ['TERMINFO'] = os.path.join(HOME, tname)
    p = subprocess.Popen(
        [tic, '-x', '-o', os.path.join(base, tname), os.path.join(base, '.terminfo', 'kitty.terminfo')],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    output = p.stdout.read()
    rc = p.wait()
    if rc != 0:
        getattr(sys.stderr, 'buffer', sys.stderr).write(output)
        raise SystemExit('Failed to compile the terminfo database')


def iter_base64_data(f):
    global leading_data
    started = 0
    while True:
        line = f.readline().rstrip()
        if started == 0:
            if line == b'KITTY_DATA_START':
                started = 1
            else:
                leading_data += line
        elif started == 1:
            if line == b'OK':
                started = 2
            else:
                raise SystemExit(line.decode('utf-8', 'replace').rstrip())
        else:
            if line == b'KITTY_DATA_END':
                break
            yield line


@contextlib.contextmanager
def temporary_directory(dir, prefix):
    # tempfile.TemporaryDirectory not available in python2
    tdir = tempfile.mkdtemp(dir=dir, prefix=prefix)
    try:
        yield tdir
    finally:
        shutil.rmtree(tdir)


def get_data():
    global data_dir, shell_integration_dir, leading_data
    data = []
    data = b''.join(iter_base64_data(tty_file_obj))
    if leading_data:
        # clear current line as it might have things echoed on it from leading_data
        # because we only turn off echo in this script whereas the leading bytes could
        # have been sent before the script had a chance to run
        sys.stdout.write('\r\033[K')
    data = base64.standard_b64decode(data)
    with temporary_directory(dir=HOME, prefix='.kitty-ssh-kitten-untar-') as tdir, tarfile.open(fileobj=io.BytesIO(data)) as tf:
        try:
            # We have to use fully_trusted as otherwise it refuses to extract,
            # for example, symlinks that point to absolute paths outside
            # tdir.
            tf.extractall(tdir, filter='fully_trusted')
        except TypeError:
            tf.extractall(tdir)
        with open(tdir + '/data.sh') as f:
            env_vars = f.read()
        apply_env_vars(env_vars)
        data_dir = os.environ.pop('KITTY_SSH_KITTEN_DATA_DIR')
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(HOME, data_dir)
        data_dir = os.path.abspath(data_dir)
        shell_integration_dir = os.path.join(data_dir, 'shell-integration')
        compile_terminfo(tdir + '/home')
        move(tdir + '/home', HOME)
        if os.path.exists(tdir + '/root'):
            move(tdir + '/root', '/')


def exec_with_better_error(*a):
    try:
        os.execlp(*a)
    except OSError as err:
        if err.errno == errno.ENOENT:
            raise SystemExit('The program: "' + a[0] + '" was not found')
        raise


def exec_zsh_with_integration():
    zdotdir = os.environ.get('ZDOTDIR') or ''
    if not zdotdir:
        zdotdir = HOME
        os.environ.pop('KITTY_ORIG_ZDOTDIR', None)  # ensure this is not propagated
    else:
        os.environ['KITTY_ORIG_ZDOTDIR'] = zdotdir
    # dont prevent zsh-newuser-install from running
    for q in ('.zshrc', '.zshenv', '.zprofile', '.zlogin'):
        if os.path.exists(os.path.join(zdotdir, q)):
            os.environ['ZDOTDIR'] = shell_integration_dir + '/zsh'
            exec_with_better_error(login_shell, os.path.basename(login_shell), '-l')
    os.environ.pop('KITTY_ORIG_ZDOTDIR', None)  # ensure this is not propagated


def exec_fish_with_integration():
    if not os.environ.get('XDG_DATA_DIRS'):
        os.environ['XDG_DATA_DIRS'] = shell_integration_dir
    else:
        os.environ['XDG_DATA_DIRS'] = shell_integration_dir + ':' + os.environ['XDG_DATA_DIRS']
    os.environ['KITTY_FISH_XDG_DATA_DIR'] = shell_integration_dir
    exec_with_better_error(login_shell, os.path.basename(login_shell), '-l')


def exec_bash_with_integration():
    os.environ['ENV'] = os.path.join(shell_integration_dir, 'bash', 'kitty.bash')
    os.environ['KITTY_BASH_INJECT'] = '1'
    if not os.environ.get('HISTFILE'):
        os.environ['HISTFILE'] = os.path.join(HOME, '.bash_history')
        os.environ['KITTY_BASH_UNEXPORT_HISTFILE'] = '1'
    exec_with_better_error(login_shell, os.path.basename('login_shell'), '--posix')


def exec_with_shell_integration():
    shell_name = os.path.basename(login_shell).lower()
    if shell_name == 'zsh':
        exec_zsh_with_integration()
    if shell_name == 'fish':
        exec_fish_with_integration()
    if shell_name == 'bash':
        exec_bash_with_integration()


def install_kitty_bootstrap():
    kitty_remote = os.environ.pop('KITTY_REMOTE', '')
    kitty_exists = shutil.which('kitty')
    kitty_dir = os.path.join(data_dir, 'kitty', 'bin')
    os.environ['SSH_KITTEN_KITTY_DIR'] = kitty_dir
    if kitty_remote == 'yes' or (kitty_remote == 'if-needed' and not kitty_exists):
        if kitty_exists:
            os.environ['PATH'] = kitty_dir + os.pathsep + os.environ['PATH']
        else:
            os.environ['PATH'] = os.environ['PATH'] + os.pathsep + kitty_dir


def main():
    global tty_file_obj, login_shell
    # the value of O_CLOEXEC below is on macOS which is most likely to not have
    # os.O_CLOEXEC being still stuck with python2
    tty_file_obj = os.fdopen(os.open(os.ctermid(), os.O_RDWR | getattr(os, 'O_CLOEXEC', 16777216)), 'rb')
    try:
        if request_data:
            set_echo(tty_file_obj.fileno(), on=False)
            send_data_request()
        get_data()
    finally:
        cleanup()
    cwd = os.environ.pop('KITTY_LOGIN_CWD', '')
    install_kitty_bootstrap()
    if cwd:
        try:
            os.chdir(cwd)
        except Exception as err:
            print(f'Failed to change working directory to: {cwd} with error: {err}', file=sys.stderr)
    ksi = frozenset(filter(None, os.environ.get('KITTY_SHELL_INTEGRATION', '').split()))
    exec_cmd = b'EXEC_CMD'
    if exec_cmd:
        os.environ.pop('KITTY_SHELL_INTEGRATION', None)
        cmd = base64.standard_b64decode(exec_cmd).decode('utf-8')
        exec_with_better_error(login_shell, os.path.basename(login_shell), '-c', cmd)
    TEST_SCRIPT  # noqa
    if ksi and 'no-rc' not in ksi:
        exec_with_shell_integration()
    os.environ.pop('KITTY_SHELL_INTEGRATION', None)
    exec_with_better_error(login_shell, '-' + os.path.basename(login_shell))


main()
