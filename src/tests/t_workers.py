from k5test import *

# Characterization tests for krb5kdc's -w numworkers option (including
# "-w auto", which detects the worker count from the CPUs available to
# the process), which had no test coverage before this file.  -w only
# takes effect once krb5kdc daemonizes, so these tests start it
# without -n via start_kdc_daemon() and assert against the KDC log
# (kdc.log), since the daemonized process's stdout is redirected away
# before we can read it, and it isn't a child process we can wait()
# on.

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

# Derive the count "-w auto" should settle on independently of the KDC.
# Matching the full "creating %d worker processes" wording matters: a bare
# 'creating' also matches the "creating worker processes" failure message
# kdc_err() writes to the same log, so a KDC that failed to fork would
# otherwise pass.
try:
    auto_expected = min(len(os.sched_getaffinity(0)), 64)
except (AttributeError, OSError):
    auto_expected = None

mark('-w auto detects a worker count and serves requests')
mark_pos = logend()
d = realm.start_kdc_daemon(['-w', 'auto'])
if auto_expected is None:
    skipped('-w auto worker-count assertion', 'cannot read CPU affinity')
else:
    needle = ('running single-process' if auto_expected <= 1
              else 'creating %d worker processes' % auto_expected)
    if wait_for_log_message(logfile, needle, mark_pos) is None:
        fail('KDC did not log the expected -w auto decision: %s' % needle)
realm.kinit(realm.user_princ, password('user'))
stop_kdc_daemon(d)

# The remaining -w auto tests need taskset to control how many CPUs the
# KDC sees, so that the detected count is deterministic.  Skip loudly
# (rather than silently asserting nothing) if it isn't available.
taskset = which('taskset')
try:
    have_cpus_01 = {0, 1} <= os.sched_getaffinity(0)
except (AttributeError, OSError):
    have_cpus_01 = False
if taskset is None:
    skipped('-w auto CPU-count tests', 'taskset not found')
elif not have_cpus_01:
    skipped('-w auto CPU-count tests',
            'CPUs 0 and 1 are not both in this process\'s affinity mask')
else:
    mark('-w auto with a single available CPU runs single-process')
    mark_pos = logend()
    d = realm.start_kdc_daemon(['-w', 'auto'], prefix=[taskset, '-c', '0'])
    if wait_for_log_message(logfile, 'running single-process',
                            mark_pos) is None:
        fail('KDC did not log single-process mode under taskset -c 0')
    stop_kdc_daemon(d)

    mark('-w auto with two available CPUs creates two workers')
    mark_pos = logend()
    d = realm.start_kdc_daemon(['-w', 'auto'], prefix=[taskset, '-c', '0-1'])
    if wait_for_log_message(logfile, 'creating 2 worker processes',
                            mark_pos) is None:
        fail('KDC did not create exactly two workers under taskset -c 0-1')
    stop_kdc_daemon(d)

# Regression test for the LMDB cap wrongly applying to a non-LMDB (here,
# db2) realm.  The realm below sets max_readers deliberately low while
# using the db2 backend, which has no reader table and no such limit:
# "-w auto" must ignore the setting entirely and land on exactly the
# affinity-derived count.  Leaving max_readers unset would not test
# anything here, since an LMDB-shaped default of 126 minus the reserve
# still exceeds the CPU count on any ordinary host, so a KDC that wrongly
# applied it would pass unnoticed.
mark('-w auto is not capped by LMDB reasoning for a db2 realm')
try:
    affinity_cpus = min(len(os.sched_getaffinity(0)), 64)
except (AttributeError, OSError):
    affinity_cpus = 0
if affinity_cpus <= 1:
    skipped('-w auto db2-not-capped-by-LMDB test',
            'host has too few CPUs (%d) available to this process to '
            'distinguish single-process fallback from a real count' %
            affinity_cpus)
else:
    realm.stop()
    realm = K5Realm(start_kdc=False, get_creds=False, create_user=False,
                    create_host=False,
                    kdc_conf={'logging': {'debug': 'true'},
                              'dbmodules': {'db': {'max_readers': '10'}}})
    mark_pos = logend()
    d = realm.start_kdc_daemon(['-w', 'auto'])
    if wait_for_log_message(logfile,
                            'creating %d worker processes' % affinity_cpus,
                            mark_pos) is None:
        fail('KDC (db2 backend) was unexpectedly capped below the '
             'affinity-derived worker count (%d) by a max_readers setting '
             'that does not apply to db2' % affinity_cpus)
    stop_kdc_daemon(d)

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

# "-w auto" must also cap the worker count using the LMDB backend's
# max_readers setting, but only for a realm that actually uses LMDB: each
# worker process is a full KDC process, and holds a reader slot in every
# LMDB environment it has open, so forking more workers than max_readers
# (less a reserve for kadmind/kpropd/kadmin.local) can support risks
# MDB_READERS_FULL at runtime.  This needs klmdb.so to actually be built;
# skip loudly (not silently) if it isn't.  This is last in the file since
# it replaces "realm" with an LMDB-backed one (freeing up its testdir and
# ports first, the same way other multi-realm *.py tests do), and nothing
# after this point needs the db2 realm used above.
if runenv.have_lmdb != 'yes':
    skipped('-w auto LMDB max_readers cap test', 'LMDB support not built')
else:
    # Mirrors LMDB_READER_RESERVE in kdc/main.c, only so this test can
    # compute the worker count "-w auto" is expected to settle on; the
    # test doesn't otherwise rely on the KDC's internals.
    lmdb_reserve = 4
    lmdb_max_readers = 10
    lmdb_expected = lmdb_max_readers - lmdb_reserve

    try:
        affinity_cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity_cpus = 0

    if affinity_cpus <= lmdb_expected:
        skipped('-w auto LMDB max_readers cap test',
                'host has too few CPUs (%d) available to this process to '
                'distinguish an LMDB max_readers-capped count (%d) from '
                'the affinity-derived one' % (affinity_cpus, lmdb_expected))
    else:
        mark('-w auto is capped by LMDB max_readers')
        realm.stop()
        realm = K5Realm(start_kdc=False, get_creds=False, create_user=False,
                        create_host=False,
                        kdc_conf={'logging': {'debug': 'true'},
                                  'dbmodules': {'db': {
                                      'db_library': 'klmdb',
                                      'max_readers': str(lmdb_max_readers),
                                      'nosync': 'true'}}})
        mark_pos = logend()
        d = realm.start_kdc_daemon(['-w', 'auto'])
        if wait_for_log_message(logfile, 'creating %d worker processes' %
                                lmdb_expected, mark_pos) is None:
            fail('KDC did not cap the auto-detected worker count to LMDB '
                 'max_readers (max_readers=%d, expected %d workers with '
                 '%d CPUs available)' %
                 (lmdb_max_readers, lmdb_expected, affinity_cpus))
        stop_kdc_daemon(d)

        # max_readers at or below the reserve leaves no budget at all, so
        # the KDC must fall back to a single process rather than being
        # raised to some minimum that would exceed the cap.
        mark('-w auto runs single-process when max_readers is tiny')
        realm.stop()
        realm = K5Realm(start_kdc=False, get_creds=False, create_user=False,
                        create_host=False,
                        kdc_conf={'logging': {'debug': 'true'},
                                  'dbmodules': {'db': {
                                      'db_library': 'klmdb',
                                      'max_readers': str(lmdb_reserve),
                                      'nosync': 'true'}}})
        mark_pos = logend()
        d = realm.start_kdc_daemon(['-w', 'auto'])
        if wait_for_log_message(logfile, 'running single-process',
                                mark_pos) is None:
            fail('KDC did not fall back to single-process with '
                 'max_readers=%d' % lmdb_reserve)
        stop_kdc_daemon(d)

success('KDC worker process tests')
