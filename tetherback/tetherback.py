#!/usr/bin/env python3
#
# Inspired by https://gist.github.com/inhies/5069663
#
# Currently backs up /data, /system, and /boot partitions
# Excludes /data/media*, just as TWRP does

import subprocess as sp
import os, sys, datetime, socket, time, argparse, re
from sys import stderr
from base64 import standard_b64decode as b64dec
from progressbar import ProgressBar, Percentage, ETA, FileTransferSpeed, Bar, DataSize
from tabulate import tabulate
from enum import Enum
from hashlib import md5
from collections import namedtuple, OrderedDict as odict

from .adb_wrapper import AdbWrapper
from .adb_stuff import *

adbxp = Enum('AdbTransport', 'tcp pipe_xo pipe_b64 pipe_bin')

p = argparse.ArgumentParser(description='''Tool to create TWRP and nandroid-style backups of an Android device running TWRP recovery, using adb-over-USB, without touching the device's internal storage or SD card.''')
p.add_argument('-s', dest='specific', metavar='DEVICE_ID', default=None, help="Specific device ID (shown by adb devices). Default is sole USB-connected device.")
p.add_argument('-o', '--output-path', default=".", help="Set optional output path for backup files.")
p.add_argument('-N', '--nandroid', action='store_true', help="Make nandroid backup; raw images rather than tarballs for /system and /data partitions (default is TWRP backup)")
p.add_argument('-0', '--dry-run', action='store_true', help="Just show the partition map and backup plan, then exit.")
p.add_argument('-V', '--no-verify', dest='verify', default=True, action='store_false', help="Don't record and verify md5sum of backup files (default is to verify).")
p.add_argument('-v', '--verbose', action='count', default=0)
p.add_argument('-f', '--force', action='store_true', help="DANGEROUS! DO NOT USE! (Tries to proceed even if TWRP recovery is not detected.)")
g = p.add_argument_group('Data transfer methods',
                         description="The default is --exec-out with adb v1.0.32 or newer, and --tcp with older versions. If you have problems, please try --base64 for a slow but reliable transfer method (and report issues at http://github.com/dlenski/tetherback/issues)")
x = g.add_mutually_exclusive_group()
x.add_argument('-t','--tcp', dest='transport', action='store_const', const=adbxp.tcp, default=None,
               help="ADB TCP forwarding (fast, should work with any host OS, but prone to timing problems)")
x.add_argument('-x','--exec-out', dest='transport', action='store_const', const=adbxp.pipe_xo,
               help="ADB exec-out binary pipe (should work with any host OS, but only with newer versions of adb and TWRP)")
x.add_argument('-6','--base64', dest='transport', action='store_const', const=adbxp.pipe_b64,
               help="Base64 pipe (very slow, should work with any host OS)")
x.add_argument('-P','--pipe', dest='transport', action='store_const', const=adbxp.pipe_bin,
               help="ADB shell binary pipe (fast, but probably only works on Linux hosts)")
g = p.add_argument_group('Backup contents')
g.add_argument('-M', '--media', action='store_true', default=False, help="Include /data/media* in TWRP backup")
g.add_argument('-D', '--data-cache', action='store_true', default=False, help="Include /data/*-cache in TWRP backup")
g.add_argument('-R', '--recovery', action='store_true', default=False, help="Include recovery partition in backup")
g.add_argument('-C', '--cache', dest='cache', action='store_true', default=False, help="Include /cache partition in backup")
g.add_argument('-U', '--no-userdata', dest='userdata', action='store_false', default=True, help="Omit /data partition from backup")
g.add_argument('-S', '--no-system', dest='system', action='store_false', default=True, help="Omit /system partition from backup")
g.add_argument('-B', '--no-boot', dest='boot', action='store_false', default=True, help="Omit boot partition from backup")
g.add_argument('-X', '--extra', action='append', dest='extra', metavar='NAME', default=[], help="Include extra partition as raw image")
args = p.parse_args()

adb = AdbWrapper('adb', ('-s',args.specific) if args.specific else ('-d',))

########################################

# check ADB version compatibility
try:
    adbversions, adbversion = adb.get_version()
except FileNotFoundError:
    p.error("could not call adb binary -- is it in your PATH?\n\thttp://developer.android.com/tools/help/adb.html")
except (sp.CalledProcessError, RuntimeError):
    p.error("could not determine ADB version")

if adbversion<(1,0,31):
    p.error("found ADB version %s, but version >= 1.0.31 is required" % adbversions)
else:
    print("Found ADB version %s" % adbversions, file=stderr)

if args.transport==adbxp.pipe_xo and adbversion<(1,0,32):
    print("WARNING: exec-out pipe (--exec-out) won't work with ADB version < 1.0.32; changing to TCP" % adbversions, file=stderr)
    args.transport = adbxp.tcp
elif args.transport==adbxp.pipe_bin:
    if adbversion>=(1,0,32):
        print("WARNING: adb shell pipe (--pipe) not needed with ADB >= 1.0.32; changing to adb exec-out pipe", file=stderr)
        args.transport = adbxp.pipe_xo
    elif sys.platform.startswith('linux'):
        print("WARNING: adb shell pipe (--pipe) only works on Linux host; changing to TCP", file=stderr)
        args.transport = adbxp.tcp
elif args.transport is None:
    if adbversion>=(1,0,32):
        print("Using default transfer method: adb exec-out pipe (--exec-out)", file=stderr)
        args.transport = adbxp.pipe_xo
    else:
        print("Using default transfer method: adb TCP forwarding (--tcp)", file=stderr)
        args.transport = adbxp.tcp

########################################

# check that device is booted into TWRP
kernel = adb.check_output(('shell','uname -r')).strip()
print("Device reports kernel %s" % kernel, file=stderr)
output = adb.check_output(('shell','twrp -v')).strip()
m = re.search(r'TWRP version ((?:\d+.)+\d+)', output)
if not m and args.force:
    print("********************")
    print("Device does not appear to be in TWRP recovery, but you specified --force")
    print("If you try to run a backup while booted in the Android OS:")
    print("  - You will probably get errors.")
    print("  - Even if the backup runs without error, it is likely to be corrupted")
    print("Unless you are developing or debugging %s, don't use this." % p.prog)
    print("********************")
    if input("Really proceed (y/N)? ")[:1].lower() != 'y':
        raise SystemExit(1)
elif not m:
    print(output)
    p.error("Device is not in TWRP; please boot into TWRP recovery and retry.")
else:
    print("Device reports TWRP version %s" % m.group(1), file=stderr)

########################################

PartInfo = namedtuple('PartInfo', 'partname devname partn size mountpoint fstype')

# build partition map
partmap = odict()
fstab = fstab_dict(adb, '/etc/fstab')
d = uevent_dict(adb, '/sys/block/mmcblk0/uevent')
nparts = int(d['NPARTS'])
print("Reading partition map for mmcblk0 (%d partitions)..." % nparts, file=stderr)
pbar = ProgressBar(max_value=nparts, widgets=['  partition map: ', Percentage(), ' ', ETA()]).start()
for ii in range(1, nparts+1):
    d = uevent_dict(adb, '/sys/block/mmcblk0/mmcblk0p%d/uevent'%ii)
    devname, partn = d['DEVNAME'], int(d['PARTN'])
    size = int(adb.check_output(('shell','cat /sys/block/mmcblk0/mmcblk0p%d/size'%ii)))
    mountpoint, fstype = fstab.get('/dev/block/%s'%d['DEVNAME'], (None, None))

    # some devices have uppercase names, see #14
    partname = d['PARTNAME'].lower()

    # some devices apparently use non-standard partition names, though standard mount points, see #18
    if partname=='system' or mountpoint=='/system':
        standard = 'system'
    elif partname=='userdata' or mountpoint=='/data':
        standard = 'userdata'
    elif partname=='cache' or mountpoint=='/cache':
        standard = 'cache'
    else:
        standard = partname

    partmap[standard] = PartInfo(partname, devname, partn, size, mountpoint, fstype)
    pbar.update(ii)
else:
    pbar.finish()

########################################

def backup_how(devname, bp):
    if devname not in bp:
        return [None, None]
    else:
        fn, mount, taropts = bp[devname]
        if mount:
            return [fn, "tar -czC %s %s" % (mount, taropts)]
        else:
            return [fn, "gzipped raw image"]

BackupPlan = namedtuple('BackupPlan', 'fn mount taropts')

# Build table of partitions requested for backup
if args.nandroid:
    rp = args.extra + [x for x in ('boot','recovery','system','userdata','cache') if getattr(args, x)]
    backup_partitions = odict((p,BackupPlan('%s.tar.gz'%p, None, None)) for p in rp)
else:
    rp = args.extra + [x for x in ('boot','recovery') if getattr(args, x)]
    backup_partitions = odict((p,BackupPlan('%s.emmc.win'%p, None, None)) for p in rp)
    mp = [x for x in ('cache','system') if getattr(args, x)]
    backup_partitions.update((p,BackupPlan('%s.ext4.win'%p, '/%s'%p, '-p')) for p in mp)

    if args.userdata:
        data_omit = []
        if not args.media: data_omit.append("media*")
        if not args.data_cache: data_omit.append("*-cache")
        backup_partitions['userdata'] = BackupPlan('data.ext4.win', '/data', '-p'+''.join(' --exclude="%s"'%x for x in data_omit))

# check that all partitions intended for backup exist
missing = set(backup_partitions) - set(partmap)

# print partition map and backup explanation
if args.dry_run or missing or args.verbose > 0:
    print("\nPartition map:\n")
    print(tabulate( [[ p.devname, p.partname + (' (standard %s)'%standard if p.partname!=standard else ''), p.size//2, p.mountpoint, p.fstype] for standard, p in partmap.items() ]
                    +[[ '', 'Total:', sum(p.size//2 for p in partmap.values()), '', '' ]],
                    [ 'BLOCK DEVICE','PARTITION NAME','SIZE (KiB)','MOUNT POINT','FSTYPE' ] ))

    print("\nBackup plan:\n")
    print(tabulate( [[standard] + backup_how(standard, backup_partitions) for standard in backup_partitions ],
                    [ 'PARTITION NAME','FILENAME','FORMAT' ] ))
    print()

if missing:
    p.error("These partitions were requested for backup, but not found in the partition map: %s" % ', '.join(missing))

if args.dry_run:
    p.exit()

########################################

# Okay, now it's time to actually... back up the partitions!

# Create the backup directory
if not os.path.exists(args.output_path):
   print("Creating backup directory %s" % args.output_path, file=stderr)
   os.mkdir(args.output_path)

backupdir = os.path.join(args.output_path, ("nandroid-backup-" if args.nandroid else "twrp-backup-") + datetime.datetime.now().strftime('%Y-%m-%d--%H-%M-%S'))
os.mkdir(backupdir)
os.chdir(backupdir)
print("Saving backup images in %s/ ..." % backupdir, file=stderr)

# Create a FIFO for device-side md5 generation
if args.verify:
    adb.check_call(('shell','rm -f /tmp/md5in; mknod /tmp/md5in p'), stderr=sp.DEVNULL)

for standard, (fn, mount, taropts) in backup_partitions.items():
    partname, devname, partn, size, mountpoint, fstype = partmap[standard]

    if mount:
        print("Saving tarball of %s (mounted at %s), %d MiB uncompressed..." % (devname, mount, size/2048))
        fstype = really_mount(adb, '/dev/block/'+devname, mount)
        if not fstype:
            raise RuntimeError('%s: could not mount %s' % (partname, mount))
        elif fstype != 'ext4':
            raise RuntimeError('%s: expected ext4 filesystem, but found %s' % (partname, fstype))
        cmdline = 'tar -czC %s %s . 2> /dev/null' % (mount, taropts or '')
    else:
        print("Saving partition %s (%s), %d MiB uncompressed..." % (partname, devname, size/2048))
        if not really_umount(adb, '/dev/block/'+devname, mount):
            raise RuntimeError('%s: could not unmount %s' % (partname, mount))
        cmdline = 'dd if=/dev/block/%s 2> /dev/null | gzip -f' % devname

    if args.verify:
        cmdline = 'md5sum /tmp/md5in > /tmp/md5out & %s | tee /tmp/md5in' % cmdline
        localmd5 = md5()

    if args.transport == adbxp.pipe_bin:
        # need stty -onlcr to make adb-shell an 8-bit-clean pipe: http://stackoverflow.com/a/20141481/20789
        child = adb.pipe_out(('shell','stty -onlcr && '+cmdline))
        block_iter = iter(lambda: child.stdout.read(65536), b'')
    elif args.transport == adbxp.pipe_b64:
        # pipe output through base64: excruciatingly slow
        child = adb.pipe_out(('shell',cmdline+'| base64'))
        block_iter = iter(lambda: b64dec(b''.join(child.stdout.readlines(65536))), b'')
    elif args.transport == adbxp.pipe_xo:
        # use adb exec-out, which is
        # (a) only available with newer versions of adb on the host, and
        # (b) only works with newer versions of TWRP (works with 2.8.0 for @kerlerm)
        # https://plus.google.com/110558071969009568835/posts/Ar3FdhknHo3
        # https://android.googlesource.com/platform/system/core/+/5d9d434efadf1c535c7fea634d5306e18c68ef1f/adb/commandline.c#1244
        child = adb.pipe_out(('exec-out',cmdline))
        block_iter = iter(lambda: child.stdout.read(65536), b'')
    else:
        port = really_forward(adb, 5600+partn, 5700+partn)
        if not port:
            raise RuntimeError('%s: could not ADB-forward a TCP port')
        child = adb.pipe_out(('shell',cmdline + '| nc -l -p%d -w3'%port))

        # FIXME: need a better way to check that socket is ready to transmit
        time.sleep(1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('localhost', port))
        block_iter = iter(lambda: s.recv(65536), b'')

    pbwidgets = ['  %s: ' % fn, Percentage(), ' ', ETA(), ' ', FileTransferSpeed(), ' ', DataSize() ]
    pbar = ProgressBar(max_value=size*512, widgets=pbwidgets).start()

    with open(fn, 'wb') as out:
        for block in block_iter:
            out.write(block)
            if args.verify:
                localmd5.update(block)
            pbar.update(out.tell())
        else:
            pbar.max_value = out.tell() or pbar.max_value # need to adjust for the smaller compressed size
            pbar.finish()

    if args.verify:
        devicemd5 = adb.check_output(('shell','cat /tmp/md5out')).strip().split()[0]
        localmd5 = localmd5.hexdigest()
        if devicemd5 != localmd5:
            raise RuntimeError("md5sum mismatch (local %s, device %s)" % (localmd5, devicemd5))
        with open(fn+'.md5', 'w') as md5out:
            print('%s *%s' % (localmd5, fn), file=md5out)

    child.wait()
    if args.transport==adbxp.tcp:
        s.close()
        if not really_unforward(adb, port):
            raise RuntimeError('could not remove ADB-forward for TCP port %d' % port)
