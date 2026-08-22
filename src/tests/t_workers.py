from k5test import *

# Characterization tests for krb5kdc's -w numworkers option, which had
# no test coverage before this file.  -w only takes effect once
# krb5kdc daemonizes, so these tests start it without -n via
# start_kdc_daemon() and assert against the KDC log (kdc.log), since
# the daemonized process's stdout is redirected away before we can
# read it, and it isn't a child process we can wait() on.

realm = K5Realm(start_kdc=False, get_creds=False,
                kdc_conf={'logging': {'debug': 'true'}})
logfile = os.path.join(realm.testdir, 'kdc.log')


def logend():
    return os.path.getsize(logfile) if os.path.exists(logfile) else 0


mark('-w starts and serves')
mark_pos = logend()
d = realm.start_kdc_daemon(['-w', '2'])
if wait_for_log_message(logfile, 'creating 2 worker processes',
                        mark_pos) is None:
    fail('KDC did not log the expected worker count')
realm.kinit(realm.user_princ, password('user'))
stop_kdc_daemon(d)

mark('-w with invalid worker counts')
for badarg in ('0', '-1', 'notanumber'):
    realm.run([krb5kdc, '-w', badarg], expected_code=1, expected_msg='usage:')

mark('-w propagates SIGHUP to workers')
mark_pos = logend()
d = realm.start_kdc_daemon(['-w', '2'])
if wait_for_log_message(logfile, 'creating 2 worker processes',
                        mark_pos) is None:
    fail('KDC did not log the expected worker count')
mark_pos = logend()
# One SIGHUP must be enough.  The supervisor blocks the signals it forwards
# and waits in sigsuspend(), so a HUP delivered at any point after the fork
# loop is guaranteed to be observed rather than missed; requiring a single
# signal here makes this a regression test for that guarantee.
os.kill(d.pid, signal.SIGHUP)
if wait_for_log_message(logfile, 'Got signal to reset', mark_pos,
                        count=2) is None:
    fail('Both worker processes did not report handling SIGHUP')
stop_kdc_daemon(d)

mark('-w terminates siblings when a worker crashes')
mark_pos = logend()
d = realm.start_kdc_daemon(['-w', '2'])
if wait_for_log_message(logfile, 'creating 2 worker processes',
                        mark_pos) is None:
    fail('KDC did not log the expected worker count')

# Find the worker pids by parentage, since the log doesn't report them
# until after they exit.
try:
    out = subprocess.check_output(['ps', '--ppid', str(d.pid), '-o', 'pid='],
                                  universal_newlines=True)
    workers = [int(p) for p in out.split()]
except (OSError, subprocess.CalledProcessError):
    workers = None
if workers is not None and len(workers) != 2:
    fail('KDC forked %d workers for -w 2, not 2' % len(workers))
if workers is None:
    skipped('worker crash propagation test', 'cannot enumerate worker pids')
    stop_kdc_daemon(d)
else:
    victim, sibling = workers
    mark_pos = logend()
    os.kill(victim, signal.SIGKILL)
    if wait_for_log_message(logfile, 'exited with status', mark_pos) is None:
        fail('KDC did not log the worker crash')
    if not d.wait_exit():
        fail('Supervisor did not exit after a worker crashed')
    if not wait_for_pid_exit(sibling, 5):
        fail('Supervisor did not terminate the sibling worker')
    forget_kdc_daemon(d)

success('KDC worker process tests')
