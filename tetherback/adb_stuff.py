from sys import stderr
import time

def really_mount(adb, dev, node, mode='ro'):
    for opts in (mode, 'remount,'+mode):
        if adb.check_output(('shell','mount -o %s %s %s 2>/dev/null && echo ok' % (opts, dev, node))).strip():
            break
    for l in adb.check_output(('shell','mount')).splitlines():
        f = l.split()
        if not l:
            pass
        elif len(f)<4:
            print( "WARNING: don't understand output from mount: %s" % (repr(l)), file=stderr )
        else:
            mdev, mnode, mtype = (f[0], f[2], f[4]) if (f[1], f[3])==('on','type') else (f[0], f[1], f[2])
            if mdev==dev or mnode==node:
                return mtype

def really_umount(adb, dev, node):
    for opts in ('','-f','-l','-r'):
        if adb.check_output(('shell','umount %s 2>/dev/null && echo ok' % dev)).strip():
            break
        if adb.check_output(('shell','umount %s 2>/dev/null && echo ok' % node)).strip():
            break
    for l in adb.check_output(('shell','mount')).splitlines():
        f = l.split()
        if not l:
            pass
        elif len(f)<4:
            print( "WARNING: don't understand output from mount: %s" % (repr(l)), file=stderr )
        else:
            mdev, mnode = (f[0], f[2]) if (f[1], f[3])==('on','type') else (f[0], f[1])
            if mdev==dev or mnode==node:
                return False
    return True

def really_forward(adb, port1, port2):
    for port in range(port1, port2):
        if adb.call(('forward','tcp:%d'%port,'tcp:%d'%port))==0:
            return port
        time.sleep(1)

def really_unforward(adb, port, tries=3):
    for retry in range(tries):
        if adb.call(('forward','--remove','tcp:%d'%port))==0:
            return retry+1
        time.sleep(1)

def uevent_dict(adb, path):
    lines = adb.check_output(('shell','cat "%s"'%path)).splitlines()
    d = {}
    for l in lines:
        if not l:
            pass
        elif '=' not in l:
            print( "WARNING: don't understand this line from %s: %s" % (repr(path), repr(l)), file=stderr )
        else:
            k, v = l.split('=',1)
            d[k] = v
    return d

def fstab_dict(adb, path='/etc/fstab'):
    lines = adb.check_output(('shell','cat '+path)).splitlines()
    d = {}
    for l in lines:
        if not l:
            pass
        else:
            f = l.split()
            if len(f)<3:
                print( "WARNING: don't understand this line from %s: %s" % (repr(path), repr(l)), file=stderr )
            else:
                # devname -> (mountpoint, fstype)
                d[f[0]] = (f[1], f[2])
    return d
