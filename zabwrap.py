import subprocess
import argparse

#todo zabbix reporting
#list of current backup types and retentions in zfs-autobackup notation
backupTypes = {"bks":"370,1d1y", "r2":"650,1h10d,1d1y", "r1":"650,1h10d,1d1y", "sandbox":"250,1h,10d", "scratch":""}

#backup settings

#backup command:
#need to add a flag for verbose / logging options
cmd1 = "/usr/local/bin/zfs-autobackup zab "
cmd2 = " --verbose"
cmd3 = " --keep-source "
cmd4 = " --ssh-target "
cmd5 = " --keep-target "
cmd6 = " --exclude-unchanged"
log = " > /var/log/zab 2>&1"

#define colors for orphans output
RED = '\033[31m'
YELLOW = '\033[33m'
RESET = '\033[0m'
GREEN = '\033[32m'

def zabwrap(dry_run, orphans, limit):
    if limit:
        result = limit
    else:
        fslist = subprocess.run(["zfs", "list", "-Hp", "-o", "name"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        fslist = fslist.stdout
        result = {}
        #get a list of zfs fs and turn it into a dictionary
        for line in fslist.split("\n"):
            parts = line.split("\n")
            fs = parts[-1]
            result[fs] = {}
            for i in range(len(parts)-2, -1, -1):
                result = {parts[i]: result}
        result = {k: v for k, v in result.items() if k != ''}

    for fs in result: #local flag ignores all inherited properties, need to hash out how we want this to behave and either keep or remove the flag. local requires manually settings on all fs
        backupsfs = subprocess.run(["zfs", "get", "-s", "local", "-H", "-o", "value", "autobackup:zab", fs], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        backupsfs = backupsfs.stdout
        if "true" in backupsfs: #is this part of the zab backup group? if yes check what type it is
            backupfstype = subprocess.run(["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            backupfstype = backupfstype.stdout
            for types in backupTypes:
                if "scratch" in backupfstype: #check if its scratch and if it is ignore it
                    if orphans:
                        print(f'{YELLOW}filesystem backup type is scratch: {RESET}'+fs)
                        break
                elif types in backupfstype: #check what server(s) this fs should be backed up to
                    backupdest = subprocess.run(["zfs", "get", "-H", "-o", "value", "zab:server", fs], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    backupdest = backupdest.stdout
                    backupdest = backupdest.strip('[ ]\n')
                    backupServers = backupdest.split(',')
                    for servers in backupServers: #generate a command for each backup destination defined with the correct backup retention
                        zab = cmd1+fs+cmd2+cmd3+backupTypes[types]+cmd4+servers+cmd5+backupTypes[types]+cmd6+log
                        if dry_run:
                            print(f'{GREEN}Backup Type:{RESET}{types} {GREEN}Command:{RESET}{zab}')
                        elif orphans:
                            break
                        else:
                            print(zab)
                            #subprocess.run([zab], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        elif orphans:
            orphanfs = subprocess.run(["zfs", "get", "-H", "-o", "value", "autobackup:zab", fs], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            orphanfs = orphanfs.stdout
            if not "true" in orphanfs:
                print(f'{RED}filesystem autobackup:zab not defined: {RESET}'+fs)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="ZFS autobackup script")
    parser.add_argument("--dry-run", "-d", action="store_true", help="print the commands to be run")
    parser.add_argument("--orphans", "-o", action="store_true", help="print a list of filesystems set to not backup")
    parser.add_argument("--limit", "-l", nargs="+", help="supply a list of filesystems to run zfs-autobackup on, must be in raid/fs format")
    args = parser.parse_args()
    zabwrap(args.dry_run, args.orphans, args.limit)
